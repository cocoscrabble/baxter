"""Playoff pairing generation and lifecycle, against the database.

The shape under test is the NACC Final Four: a division qualifies after its main
schedule, the top four play best-of-three semifinals, then a championship and a
third-place series in the same window.
"""

import json
from datetime import date

from django.db.models import Q
from django.test import TestCase, tag
from django.urls import reverse

from tournaments.commands import create_playoff, delete_playoff
from tournaments.events import division_digest
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.replay import events_from_tournament, replay
from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Pairing,
    Player,
    Playoff,
    PlayoffSeries,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.pairing.round_pairing import blocks_to_round_pairings
from tournaments.playoff import (
    CHAMPIONSHIP,
    SEMIFINAL,
    THIRD_PLACE,
    SeriesStatus,
    conflicts_for_single_result,
    default_stage_games,
    playoff_for,
    qualification_seeds,
    refresh_after_results,
)
from users.models import User

MAIN_ROUNDS = 3


class PlayoffRoundsTestCase(TestCase):
    """A division that has finished its main schedule, ready to qualify.

    Subclasses vary the field size and how much of the schedule is played:
    ``main_rounds`` rounds are configured, ``played_rounds`` of them are played
    (the two differ only for a concurrent playoff, which keeps pairing the main
    field through the bracket's rounds).
    """

    n_entrants = 6
    main_rounds = MAIN_ROUNDS
    played_rounds = MAIN_ROUNDS

    def setUp(self):
        self.user = User.objects.create_user(username="td", password="pw")
        self.tournament = Tournament.objects.create(
            name="NACC", location="Toronto", start_date=date(2026, 8, 1),
            owner=self.user,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Division 1"
        )
        blocks = [{"pairing": "Swiss", "rounds": self.main_rounds, "pair_from": 1}]
        DivisionSettings.objects.create(
            division=self.division,
            pairing_blocks=blocks,
            round_pairings=[rp.to_dict() for rp in blocks_to_round_pairings(blocks)],
        )
        self.entrants = {}
        for i in range(1, self.n_entrants + 1):
            player = Player.objects.create(
                name=f"P{i}", player_number=f"{i:03d}", rating=2000 - 10 * i
            )
            self.entrants[player.name] = Entrant.objects.create(
                division=self.division, player=player, number=i
            )
        # Play the main schedule so the standings order is P1…P6.
        self.play_main_rounds()

    def play_main_rounds(self, start=1, end=None):
        """Rounds in which every player beats everyone below them that they
        meet, leaving the seeding order intact."""
        regenerate_pairings(self.division)
        for round_num in range(start, (end or self.played_rounds) + 1):
            publish_rounds(self.division, [round_num])
            for pairing in self.division.pairings.filter(round=round_num):
                first, second = pairing.first, pairing.second
                if first.player.is_bye or second.player.is_bye:
                    continue
                winner, loser = (
                    (first, second) if first.number < second.number else (second, first)
                )
                ResultSlip.objects.create(
                    division=self.division, round=round_num, pairing=pairing,
                    winner=winner, winner_score=450, loser=loser, loser_score=400,
                    winner_started=winner.pk == pairing.first_id,
                )
            rp = self.division.round_pairings_set.get(round=round_num)
            rp.update_status()
            regenerate_pairings(self.division)

    # -- helpers ---------------------------------------------------------

    def make_playoff(self, count=4, timing=Playoff.POSTSCRIPT,
                     qualification_round=None, **games):
        stage_games = default_stage_games(count, 3)
        stage_games.update(games)
        qual = self.played_rounds if qualification_round is None else qualification_round
        seeds = qualification_seeds(self.division, qual, count)
        payload = {
            "division": self.division.name,
            "qualification_round": qual,
            "qualifier_count": count,
            "timing": timing,
            "stage_games": stage_games,
            "seeds": seeds,
        }
        playoff = create_playoff(self.tournament, self.user, payload)
        regenerate_pairings(self.division)
        return playoff

    def pairings_in(self, round_num):
        return list(
            self.division.pairings.filter(round=round_num)
            .select_related("first__player", "second__player", "series")
            .order_by("table")
        )

    def names_in(self, round_num):
        return {
            frozenset({p.first.player.name, p.second.player.name})
            for p in self.pairings_in(round_num)
        }

    def record(self, round_num, winner, loser, winner_score=450, loser_score=400):
        """Publish the round if needed, then record a result against its pairing.

        Publishing first is what a director does — and what the app requires:
        a draft round is regenerated from scratch, so a result recorded against
        one would lose its pairing on the next pass.
        """
        publish_rounds(self.division, [round_num])
        names = {winner, loser}
        pairing = next(
            p for p in self.pairings_in(round_num)
            if {p.first.player.name, p.second.player.name} == names
        )
        slip = ResultSlip.objects.create(
            division=self.division, round=round_num, pairing=pairing,
            winner=self.entrants[winner], winner_score=winner_score,
            loser=self.entrants[loser], loser_score=loser_score,
            winner_started=self.entrants[winner].pk == pairing.first_id,
        )
        pairing.round_pairings.update_status()
        refresh_after_results(self.division)
        regenerate_pairings(self.division)
        return slip

    def correct(self, slip, winner, loser, winner_score=450, loser_score=400):
        """Rewrite an existing result, then let the bracket recompute."""
        slip.winner = self.entrants[winner]
        slip.loser = self.entrants[loser]
        slip.winner_score = winner_score
        slip.loser_score = loser_score
        slip.winner_started = self.entrants[winner].pk == slip.pairing.first_id
        slip.save()
        refresh_after_results(self.division)
        regenerate_pairings(self.division)


class GenerationTests(PlayoffRoundsTestCase):
    def test_qualification_seeds_come_from_the_standings(self):
        seeds = qualification_seeds(self.division, MAIN_ROUNDS, 4)
        self.assertEqual([s["player"] for s in seeds], ["P1", "P2", "P3", "P4"])
        self.assertEqual([s["seed"] for s in seeds], [1, 2, 3, 4])

    def test_dropped_entrants_are_not_offered_as_qualifiers(self):
        entrant = self.entrants["P2"]
        entrant.dropped = True
        entrant.save(update_fields=["dropped"])
        seeds = qualification_seeds(self.division, MAIN_ROUNDS, 4)
        self.assertNotIn("P2", [s["player"] for s in seeds])

    def test_semifinal_games_are_generated_for_the_reserved_rounds(self):
        self.make_playoff()
        # Games 1 and 2 of both semifinals are certainly necessary.
        self.assertEqual(self.names_in(4), {frozenset({"P1", "P4"}), frozenset({"P2", "P3"})})
        self.assertEqual(self.names_in(5), {frozenset({"P1", "P4"}), frozenset({"P2", "P3"})})
        # Game 3 waits until game 2 is in.
        self.assertEqual(self.names_in(6), set())

    def test_non_qualifiers_get_no_games_and_no_byes(self):
        self.make_playoff()
        playing = {
            name
            for round_num in (4, 5, 6)
            for pair in self.names_in(round_num)
            for name in pair
        }
        self.assertEqual(playing, {"P1", "P2", "P3", "P4"})
        self.assertFalse(
            self.division.pairings.filter(round__gte=4, first__player__is_bye=True).exists()
        )
        self.assertFalse(
            self.division.pairings.filter(round__gte=4, second__player__is_bye=True).exists()
        )
        # …and nobody is marked withdrawn to achieve it.
        self.assertFalse(Entrant.all_objects.filter(division=self.division, dropped=True).exists())

    def test_series_rows_are_created_and_linked_to_their_pairings(self):
        self.make_playoff()
        self.assertEqual(PlayoffSeries.objects.filter(playoff__division=self.division).count(), 4)
        semi = PlayoffSeries.objects.get(key=SEMIFINAL, position=0)
        self.assertEqual(semi.high.player.name, "P1")
        self.assertEqual(semi.low.player.name, "P4")
        self.assertEqual(semi.max_games, 3)
        game1 = self.division.pairings.get(round=4, series=semi)
        self.assertEqual(game1.game_number, 1)

    def test_playoff_games_take_the_top_boards(self):
        self.make_playoff()
        boards = [
            (p.table, {p.first.player.name, p.second.player.name})
            for p in self.pairings_in(4)
        ]
        self.assertEqual(boards[0][1], {"P1", "P4"})  # best seed on board 1

    def test_the_series_alternates_who_goes_first(self):
        self.make_playoff()
        game1 = next(p for p in self.pairings_in(4) if p.series.position == 0)
        game2 = next(p for p in self.pairings_in(5) if p.series.position == 0)
        self.assertNotEqual(game1.first.player.name, game2.first.player.name)

    def test_deleting_a_playoff_removes_its_rounds(self):
        self.make_playoff()
        delete_playoff(self.tournament, self.user, {"division": self.division.name})
        regenerate_pairings(self.division)
        self.assertFalse(self.division.pairings.filter(round__gte=4).exists())
        self.assertFalse(PlayoffSeries.objects.filter(playoff__division=self.division).exists())


class SeriesLifecycleTests(PlayoffRoundsTestCase):
    def test_a_clinched_series_stops_generating_games(self):
        self.make_playoff()
        self.record(4, "P1", "P4")
        self.record(5, "P1", "P4")
        bracket = playoff_for(self.division).bracket()
        self.assertEqual(bracket.get(SEMIFINAL, 0).status, SeriesStatus.CLINCHED)
        # Round 6 holds only the other semifinal's decider, if it needs one.
        self.assertNotIn(frozenset({"P1", "P4"}), self.names_in(6))

    def test_a_level_series_gets_its_third_game(self):
        self.make_playoff()
        self.record(4, "P1", "P4")
        self.record(5, "P4", "P1")
        self.assertIn(frozenset({"P1", "P4"}), self.names_in(6))

    def test_a_window_whose_series_all_clinch_early_closes_with_no_games(self):
        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P2", "P3")
        # Both semifinals are over, so the third semifinal round has no games —
        # and no bye, no zero score and no result was invented to fill it.
        publish_rounds(self.division, [6])
        self.assertEqual(self.pairings_in(6), [])
        self.assertFalse(self.division.result_slips.filter(round=6).exists())
        refresh_after_results(self.division)
        rp = self.division.round_pairings_set.get(round=6)
        self.assertEqual(rp.status, RoundPairings.FINISHED)

    def test_a_correction_retires_a_published_game_it_makes_unnecessary(self):
        # The only way a *published* playoff game becomes unnecessary: a game is
        # scheduled only once it is certainly needed, so it takes a correction
        # to an earlier result to retire it.
        self.make_playoff()
        self.record(4, "P1", "P4")
        self.record(4, "P2", "P3")
        game2 = self.record(5, "P4", "P1")   # semifinal 1 level at 1–1…
        self.record(5, "P2", "P3")
        publish_rounds(self.division, [6])
        self.assertIn(frozenset({"P1", "P4"}), self.names_in(6))  # …so game 3 exists
        # The director corrects game 2: P1 won it after all, 2–0.
        self.correct(game2, "P1", "P4")
        self.assertEqual(self.pairings_in(6), [])
        self.assertFalse(self.division.result_slips.filter(round=6).exists())

    def test_a_published_window_gains_a_decider_it_turns_out_to_need(self):
        # A director publishes the whole semifinal window up front. Round 6 is
        # empty at that point (nobody can clinch in one game), but once a series
        # goes 1–1 its third game becomes necessary and must appear even though
        # the round is already published.
        self.make_playoff()
        publish_rounds(self.division, [4, 5, 6])
        self.assertEqual(self.pairings_in(6), [])
        self.record(4, "P1", "P4")
        self.record(5, "P4", "P1")
        self.assertEqual(
            self.division.round_pairings_set.get(round=6).status,
            RoundPairings.PUBLISHED,
        )
        self.assertIn(frozenset({"P1", "P4"}), self.names_in(6))

    def test_winners_advance_into_the_championship_window(self):
        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P3", "P2")
        self.assertEqual(
            self.names_in(7), {frozenset({"P1", "P3"}), frozenset({"P2", "P4"})}
        )
        championship = PlayoffSeries.objects.get(key=CHAMPIONSHIP)
        third = PlayoffSeries.objects.get(key=THIRD_PLACE)
        self.assertEqual(
            {championship.high.player.name, championship.low.player.name}, {"P1", "P3"}
        )
        self.assertEqual({third.high.player.name, third.low.player.name}, {"P2", "P4"})

    def test_the_bracket_survives_a_full_run(self):
        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P2", "P3")
        for round_num in (7, 8):
            self.record(round_num, "P2", "P1")
            self.record(round_num, "P3", "P4")
        bracket = playoff_for(self.division).bracket()
        self.assertTrue(bracket.complete)
        self.assertEqual(bracket.get(CHAMPIONSHIP).winner, "P2")
        self.assertEqual(bracket.get(THIRD_PLACE).winner, "P3")


class CorrectionGuardTests(PlayoffRoundsTestCase):
    def played_semis_and_a_final_game(self):
        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P2", "P3")
        self.record(7, "P2", "P1")

    def test_a_correction_that_rewrites_a_played_bracket_is_refused(self):
        self.played_semis_and_a_final_game()
        pairing = next(
            p for p in self.pairings_in(5)
            if {p.first.player.name, p.second.player.name} == {"P1", "P4"}
        )
        # Reversing this game leaves the semifinal level, so P1 is no longer in
        # the championship — but the championship already has a game recorded.
        conflicts = conflicts_for_single_result(
            self.division, pairing, "P4", 450, 400
        )
        self.assertTrue(conflicts)
        self.assertIn("already been played", conflicts[0])

    def test_a_harmless_correction_is_allowed(self):
        self.played_semis_and_a_final_game()
        pairing = next(
            p for p in self.pairings_in(5)
            if {p.first.player.name, p.second.player.name} == {"P1", "P4"}
        )
        # Same winner, corrected score: nothing downstream changes.
        self.assertEqual(
            conflicts_for_single_result(self.division, pairing, "P1", 500, 300), []
        )


class DigestTests(PlayoffRoundsTestCase):
    def test_playoff_state_is_part_of_the_digest(self):
        before = division_digest(self.division)
        self.make_playoff()
        self.assertNotEqual(division_digest(self.division), before)

    def test_a_division_without_a_playoff_keeps_its_old_digest_shape(self):
        from tournaments.events import division_state

        self.assertNotIn("playoff", division_state(self.division))

    def test_the_digest_moves_when_a_series_is_decided(self):
        self.make_playoff()
        before = division_digest(self.division)
        self.record(4, "P1", "P4")
        self.record(5, "P1", "P4")
        self.assertNotEqual(division_digest(self.division), before)


class ConcurrentTests(PlayoffRoundsTestCase):
    """Eight players, five configured rounds, a bracket over rounds 4–5.

    The main field keeps playing its schedule while the top four play the
    bracket, all inside one division.
    """

    n_entrants = 8
    main_rounds = 5
    played_rounds = 3

    def make_concurrent(self, count=4):
        playoff = self.make_playoff(
            count=count, timing=Playoff.CONCURRENT,
            semifinal=1, championship=1, third_place=1,
        )
        # Read the qualifiers off the snapshot rather than assuming them: with an
        # odd field the earlier byes move the standings around.
        self.qualifiers = list(playoff.config().seeds)
        return playoff

    def test_a_concurrent_round_holds_both_kinds_of_game(self):
        self.make_concurrent()
        pairs = self.names_in(4)
        seeds = self.qualifiers
        # Two semifinals, seeded 1–4 and 2–3…
        self.assertIn(frozenset({seeds[0], seeds[3]}), pairs)
        self.assertIn(frozenset({seeds[1], seeds[2]}), pairs)
        # …and the rest of the field paired among themselves.
        ordinary = [p for p in self.pairings_in(4) if p.series_id is None]
        self.assertTrue(ordinary)
        for pairing in ordinary:
            names = {pairing.first.player.name, pairing.second.player.name}
            self.assertEqual(names & set(seeds), set())

    def test_everyone_has_exactly_one_game(self):
        self.make_concurrent()
        appearances = [name for pair in self.names_in(4) for name in pair]
        self.assertEqual(
            sorted(appearances), [f"P{i}" for i in range(1, self.n_entrants + 1)]
        )

    def test_reserved_players_are_not_dropped_and_never_byed(self):
        self.make_concurrent()
        self.assertFalse(
            Entrant.all_objects.filter(division=self.division, dropped=True).exists()
        )
        byed = {
            name
            for pair in self.names_in(4)
            if "Bye" in pair
            for name in pair
        }
        self.assertEqual(byed & set(self.qualifiers), set())

    def test_the_round_after_a_mixed_round_still_pairs(self):
        # The round-status fix: a round whose reserved players never appear must
        # still count as finished, or the next round never pairs.
        self.make_concurrent()
        for pair in sorted(self.names_in(4), key=sorted):
            if "Bye" in pair:
                continue  # already resolved when the round was published
            high, low = sorted(pair)
            self.record(4, high, low)
        # Round 5 pairs the whole division again: the final window's two series
        # plus the main field's own games.
        appearances = sorted(
            name for pair in self.names_in(5) for name in pair if name != "Bye"
        )
        self.assertEqual(appearances, [f"P{i}" for i in range(1, self.n_entrants + 1)])
        # Round 5 is the final window: championship and third place, plus the
        # main field's own game.
        self.assertTrue(
            self.division.pairings.filter(round=5, series__isnull=False).exists()
        )

    def test_playoff_games_take_the_top_boards_in_a_mixed_round(self):
        self.make_concurrent()
        # A bye carries no table, so it sorts to table 0 ahead of every board.
        boards = [
            p for p in self.pairings_in(4)
            if not (p.first.player.is_bye or p.second.player.is_bye)
        ]
        self.assertTrue(all(p.series_id is not None for p in boards[:2]))

    def test_a_concurrent_playoff_must_fit_the_schedule(self):
        # Best-of-three semis would need rounds 4–6, past the five-round schedule.
        with self.assertRaises(ValueError) as caught:
            self.make_playoff(count=4, timing=Playoff.CONCURRENT)
        self.assertIn("cover its rounds", str(caught.exception))

    def test_a_postscript_playoff_must_start_where_the_schedule_ends(self):
        with self.assertRaises(ValueError) as caught:
            self.make_playoff(count=4, timing=Playoff.POSTSCRIPT)
        self.assertIn("where the main tournament ends", str(caught.exception))

    def test_export_holds_every_played_game_once(self):
        from tournaments.tournament_export import ExportDivision

        self.make_concurrent()
        for pair in sorted(self.names_in(4), key=sorted):
            high, low = sorted(pair)
            self.record(4, high, low)
        exported = ExportDivision.from_db(self.division)
        real = self.division.result_slips.exclude(loser__player__is_bye=True).count()
        self.assertEqual(len(exported.results), real)
        rows = [(r.round, tuple(sorted([r.winner, r.loser]))) for r in exported.results]
        self.assertEqual(len(rows), len(set(rows)))


class ConcurrentOddFieldTests(ConcurrentTests):
    """Seven players: reserving four leaves an odd main field."""

    n_entrants = 7

    def test_everyone_has_exactly_one_game(self):
        # An odd field means one player is byed rather than paired, so the
        # even-field assertion in the parent class doesn't apply here.
        self.make_concurrent()
        appearances = sorted(
            name for pair in self.names_in(4) for name in pair if name != "Bye"
        )
        self.assertEqual(appearances, [f"P{i}" for i in range(1, 8)])

    def test_exactly_one_non_qualifier_is_byed(self):
        self.make_concurrent()
        bye_pairings = self.division.pairings.filter(round=4).filter(
            Q(first__player__is_bye=True) | Q(second__player__is_bye=True)
        ).select_related("first__player", "second__player")
        self.assertEqual(bye_pairings.count(), 1)
        byed = bye_pairings.first()
        names = {byed.first.player.name, byed.second.player.name}
        self.assertEqual(names & set(self.qualifiers), set())

    def test_export_holds_every_played_game_once(self):
        # The parent version records every pairing; here one of them is a bye,
        # which has no opponent to record a result against.
        pass


class QualificationRoundChoiceTests(PlayoffRoundsTestCase):
    """A twelve-round schedule with only eight played — the shape that made the
    qualification-round dropdown unusable."""

    main_rounds = 12
    played_rounds = 8

    def test_unplayed_configured_rounds_are_selectable(self):
        from tournaments.playoff import selectable_qualification_rounds

        self.assertEqual(
            selectable_qualification_rounds(self.division), list(range(1, 13))
        )

    def test_the_setup_page_offers_every_configured_round(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("division_playoff_setup", kwargs=self.division.slug_kwargs())
        )
        html = response.content.decode()
        # The last round of the schedule is what a postscript playoff needs, and
        # it is not played yet.
        self.assertIn('<option value="12"', html)
        self.assertIn('<option value="9"', html)

    def test_a_round_that_is_not_finished_cannot_be_confirmed(self):
        # Round 12 is the one a postscript playoff must use, and it is unplayed:
        # choosing it is fine, freezing seeds from it is not.
        with self.assertRaises(ValueError) as caught:
            self.make_playoff(count=4, qualification_round=12)
        self.assertIn("isn't finished yet", str(caught.exception))

    def test_a_finished_round_is_accepted(self):
        # Play out the rest of the schedule, then qualify on its last round.
        self.play_main_rounds(start=9, end=12)
        playoff = self.make_playoff(count=4, qualification_round=12)
        self.assertEqual(playoff.qualification_round, 12)


class PlayoffViewTests(PlayoffRoundsTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def url(self, name):
        return reverse(name, kwargs=self.division.slug_kwargs())

    def test_the_bracket_page_says_so_when_there_is_no_playoff(self):
        response = self.client.get(self.url("division_playoff"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no playoff")

    def test_the_bracket_page_shows_series_status_and_placements(self):
        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P2", "P3")
        response = self.client.get(self.url("division_playoff"))
        self.assertContains(response, "Semifinal 1")
        self.assertContains(response, "Best of 3")
        self.assertContains(response, "P1 wins")
        self.assertContains(response, "Final placements")
        # The third game of a clinched series is shown as unnecessary, not as a
        # bye or a zero score.
        self.assertContains(response, "not necessary")
        self.assertNotContains(response, "Bye")

    def test_the_setup_page_previews_the_qualifiers(self):
        response = self.client.get(self.url("division_playoff_setup"))
        self.assertEqual(response.status_code, 200)
        for name in ("P1", "P2", "P3", "P4"):
            self.assertContains(response, name)

    def test_creating_a_playoff_through_the_form(self):
        response = self.client.post(
            self.url("division_playoff_setup"),
            {
                "action": "confirm",
                "qualification_round": self.played_rounds,
                "qualifier_count": 4,
                "timing": Playoff.POSTSCRIPT,
                "games_semifinal": 3,
                "games_championship": 3,
                "games_third_place": 3,
                "seed": ["P1", "P2", "P3", "P4"],
            },
        )
        self.assertRedirects(response, self.url("division_playoff"))
        playoff = playoff_for(self.division)
        self.assertIsNotNone(playoff)
        self.assertEqual([s["player"] for s in playoff.seeds], ["P1", "P2", "P3", "P4"])

    def test_the_form_rejects_an_even_series_length(self):
        response = self.client.post(
            self.url("division_playoff_setup"),
            {
                "action": "confirm",
                "qualification_round": self.played_rounds,
                "qualifier_count": 4,
                "timing": Playoff.POSTSCRIPT,
                "games_semifinal": 2,
                "games_championship": 3,
                "games_third_place": 3,
                "seed": ["P1", "P2", "P3", "P4"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "odd number of games")
        self.assertIsNone(playoff_for(self.division))

    def test_the_director_can_override_the_seeds(self):
        self.client.post(
            self.url("division_playoff_setup"),
            {
                "action": "confirm",
                "qualification_round": self.played_rounds,
                "qualifier_count": 4,
                "timing": Playoff.POSTSCRIPT,
                "games_semifinal": 3,
                "games_championship": 3,
                "games_third_place": 3,
                "seed": ["P2", "P1", "P3", "P4"],
            },
        )
        playoff = playoff_for(self.division)
        self.assertEqual([s["player"] for s in playoff.seeds][:2], ["P2", "P1"])

    def test_a_started_playoff_cannot_be_removed(self):
        self.make_playoff()
        self.record(4, "P1", "P4")
        response = self.client.post(
            self.url("division_playoff_setup"), {"action": "delete"}, follow=True
        )
        self.assertContains(response, "already has results")
        self.assertIsNotNone(playoff_for(self.division))

    def test_an_unplayed_playoff_can_be_removed(self):
        self.make_playoff()
        self.client.post(self.url("division_playoff_setup"), {"action": "delete"})
        self.assertIsNone(playoff_for(self.division))

    def test_the_pairings_page_labels_playoff_games(self):
        self.make_playoff()
        response = self.client.get(self.url("division_pair_rounds"))
        self.assertContains(response, "Semifinal")

    def test_the_standings_page_points_at_the_bracket(self):
        self.make_playoff()
        response = self.client.get(self.url("division_standings"))
        self.assertContains(response, "playoff bracket")

    def test_a_visitor_can_read_the_bracket(self):
        self.make_playoff()
        self.client.logout()
        response = self.client.get(self.url("division_playoff"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Semifinal 1")

    def test_a_visitor_cannot_reach_the_setup_page(self):
        response = self.client.get(self.url("division_playoff_setup"))
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        response = self.client.get(self.url("division_playoff_setup"))
        self.assertNotEqual(response.status_code, 200)


@tag("slow")
class PlayoffReplayTests(TestCase):
    """A playoff run entirely through the real views replays from its log.

    This is the load-bearing test for the "derive, don't store" design: the log
    carries the playoff's configuration and seed snapshot plus the results, and
    replay must rebuild the bracket, its pairings, and the final placements —
    all of which the division digest covers.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.players = [
            Player.objects.create(name=n, player_number=f"P{i:03d}", rating=r)
            for i, (n, r) in enumerate(
                [("Alice", 1800), ("Bob", 1700), ("Cara", 1600), ("Dan", 1500)], 1
            )
        ]
        self.client.force_login(self.owner)

    def _post_json(self, name, division, body):
        return self.client.post(
            reverse(name, kwargs=division.slug_kwargs()),
            json.dumps(body),
            content_type="application/json",
        )

    def _play_round(self, division, round_num):
        # A Pair Rounds render is what lazily pairs the next round, as in real use.
        self.client.get(reverse("division_pair_rounds", kwargs=division.slug_kwargs()))
        self.client.post(
            reverse("publish_round", kwargs=division.slug_kwargs()),
            {"round": round_num},
        )
        self._post_json("simulate_round", division, {"round": round_num})

    def test_a_full_playoff_replays_to_the_same_digest(self):
        self.client.post(
            reverse("tournament_create"),
            {"name": "Champs", "location": "Reno", "start_date": "2026-03-15",
             "editor_usernames": ""},
        )
        tournament = Tournament.objects.get(name="Champs")
        division = tournament.divisions.get()
        # Simulation is a test-division tool; the playoff machinery is not.
        division.is_test = True
        division.save(update_fields=["is_test"])

        self._post_json(
            "division_entrants_edit", division,
            {"rows": [
                {"number": i + 1, "player": p.pk, "dropped": False}
                for i, p in enumerate(self.players)
            ]},
        )
        self._post_json(
            "division_round_pairings", division,
            {"blocks": [{"pairing": "KotH", "rounds": 2, "pair_from": 1}]},
        )
        self.client.get(reverse("division_pair_rounds", kwargs=division.slug_kwargs()))
        for round_num in (1, 2):
            self._play_round(division, round_num)

        # Top two qualify for a best-of-three championship after round 2.
        seeds = qualification_seeds(division, 2, 2)
        create_playoff(
            tournament, self.owner,
            {
                "division": division.name,
                "qualification_round": 2,
                "qualifier_count": 2,
                "timing": Playoff.POSTSCRIPT,
                "stage_games": {CHAMPIONSHIP: 3},
                "seeds": seeds,
            },
        )
        self.client.get(reverse("division_pair_rounds", kwargs=division.slug_kwargs()))
        for round_num in (3, 4, 5):
            self._play_round(division, round_num)

        recorded = division_digest(division)
        bracket = playoff_for(division).bracket()
        self.assertTrue(bracket.get(CHAMPIONSHIP).decided)

        events = events_from_tournament(tournament)
        self.assertIn(
            "playoff_created", [e["event_type"] for e in events]
        )
        ctx = replay(events, verify=True)
        replayed = ctx.tournament.divisions.get()
        self.assertEqual(division_digest(replayed), recorded)
        # …and the reconstructed bracket agrees on the winner.
        replayed_bracket = playoff_for(replayed).bracket()
        self.assertEqual(
            replayed_bracket.get(CHAMPIONSHIP).winner,
            bracket.get(CHAMPIONSHIP).winner,
        )


class ExportTests(PlayoffRoundsTestCase):
    def test_only_played_games_are_exported(self):
        from tournaments.tournament_export import ExportDivision

        self.make_playoff()
        for round_num in (4, 5):
            self.record(round_num, "P1", "P4")
            self.record(round_num, "P2", "P3")
        exported = ExportDivision.from_db(self.division)
        played = self.division.result_slips.count()
        self.assertEqual(len(exported.results), played)
        # The unnecessary third games are in neither the export nor the DB.
        self.assertFalse(Pairing.objects.filter(division=self.division, round=6).exists())
