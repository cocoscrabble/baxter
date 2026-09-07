"""Bye handling: a synthetic bye is added at pairing time for an odd field."""

from collections import Counter
from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.fake_tournament import create_fake_tournament
from tournaments.generate_pairings import (
    BYE_WINNER_SCORE,
    publish_rounds,
    regenerate_pairings,
    unpublish_rounds,
)
from tournaments.match_simulation import simulate_round
from tournaments.models import (
    BYE_PLAYER_NUMBER,
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.pairing.base import PairingData, standings_after_round
from tournaments.pairing.round_pairing import blocks_to_round_pairings
from tournaments.tournament_export import ExportTournament
from users.models import User


def make_division(owner, n_players, rounds, pairing="KotH", first_number=1):
    """A division of ``n_players``. ``first_number`` offsets the player numbers,
    so a test that builds a second division does not collide with the first
    (player numbers are unique across the whole database)."""
    tournament = Tournament.objects.create(
        name="T", location="x", start_date=date.today(), owner=owner
    )
    division = Division.objects.create(tournament=tournament, name="D")
    for i in range(1, n_players + 1):
        number = first_number + i - 1
        Entrant.objects.create(
            division=division,
            player=Player.objects.create(
                name=f"P{number}", player_number=str(number), rating=2000 - i
            ),
            number=i,
        )
    blocks = [{"pairing": pairing, "rounds": rounds, "pair_from": 1}]
    DivisionSettings.objects.create(
        division=division,
        pairing_blocks=blocks,
        round_pairings=[rp.to_dict() for rp in blocks_to_round_pairings(blocks)],
    )
    return division


class ByePlayerModelTests(TestCase):
    def test_get_bye_is_a_singleton(self):
        first = Player.get_bye()
        again = Player.get_bye()
        self.assertEqual(first.pk, again.pk)
        self.assertTrue(first.is_bye)
        self.assertTrue(first.is_provisional)
        self.assertEqual(Player.objects.filter(is_bye=True).count(), 1)

    def test_bye_excluded_from_entrants_grid_picker(self):
        from tournaments.grids import EntrantsGrid

        user = User.objects.create_user(username="g", password="p")
        division = make_division(user, 2, 2)
        bye = Player.get_bye()

        grid = EntrantsGrid()
        players = grid.lookups(division)["players"]
        self.assertNotIn(bye.pk, [p["id"] for p in players])
        self.assertNotIn("Bye", [p["label"] for p in players])
        # The two real players are still offered.
        self.assertEqual(len(players), 2)

        valid_ids, _ = grid.validate_args(division)
        self.assertNotIn(bye.pk, valid_ids)

    def test_default_manager_hides_bye_entrant(self):
        user = User.objects.create_user(username="o", password="p")
        division = make_division(user, 3, 2)
        bye_entrant = division.bye_entrant()

        # Reverse relation (and the default manager) exclude the bye...
        self.assertEqual(division.entrants.count(), 3)
        self.assertNotIn(
            bye_entrant.pk, division.entrants.values_list("pk", flat=True)
        )
        # ...but it is reachable via all_objects.
        self.assertIn(
            bye_entrant.pk,
            Entrant.all_objects.filter(division=division).values_list("pk", flat=True),
        )


class ByePairingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="byeowner", password="p")

    def test_odd_field_creates_a_bye_pairing(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)

        round1 = list(division.pairings.filter(round=1))
        self.assertEqual(len(round1), 3)  # 2 real games + 1 bye
        byes = [
            p for p in round1 if p.first.player.is_bye or p.second.player.is_bye
        ]
        self.assertEqual(len(byes), 1)
        self.assertEqual(byes[0].table, 0)
        # No result until the round is published.
        self.assertEqual(division.result_slips.filter(round=1).count(), 0)

    def test_publishing_records_the_bye_as_a_win(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])

        slip = division.result_slips.get(round=1, loser__player__is_bye=True)
        self.assertFalse(slip.winner.player.is_bye)
        self.assertEqual(slip.winner_score - slip.loser_score, BYE_WINNER_SCORE)
        # The byed player is the notional non-starter (bye "starts").
        self.assertFalse(slip.winner_started)

    def test_publishing_an_odd_round_stays_published_not_in_progress(self):
        # The auto bye isn't a played game, so a freshly published odd round is
        # PUBLISHED, not IN_PROGRESS — only a real result moves it forward.
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)

    def test_first_real_result_moves_odd_round_to_in_progress(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        real = [
            p for p in division.pairings.filter(round=1)
            if not (p.first.player.is_bye or p.second.player.is_bye)
        ][0]
        ResultSlip.objects.create(
            division=division, round=1, pairing=real,
            winner=real.first, winner_score=450,
            loser=real.second, loser_score=380, winner_started=True,
        )
        real.round_pairings.update_status()
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.IN_PROGRESS)

    def test_unpublishing_clears_the_bye_and_reverts_to_draft(self):
        # The auto bye isn't a real result, so the round can still be unpublished
        # — and its bye slip is dropped for a clean draft.
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        self.assertTrue(
            division.result_slips.filter(round=1, loser__player__is_bye=True).exists()
        )

        unpublished = unpublish_rounds(division, [1])

        self.assertEqual(unpublished, [1])
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        self.assertFalse(division.result_slips.filter(round=1).exists())
        # Pairings survive so the round can be edited and republished.
        self.assertTrue(rp.pairings.exists())

    def test_unpublishing_blocked_by_a_real_result(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        # Enter a real game result alongside the auto bye.
        real = [
            p for p in division.pairings.filter(round=1)
            if not (p.first.player.is_bye or p.second.player.is_bye)
        ][0]
        ResultSlip.objects.create(
            division=division, round=1, pairing=real,
            winner=real.first, winner_score=450,
            loser=real.second, loser_score=380, winner_started=True,
        )

        self.assertEqual(unpublish_rounds(division, [1]), [])
        # Round and both slips are untouched.
        self.assertTrue(
            division.result_slips.filter(round=1, loser__player__is_bye=True).exists()
        )

    def test_even_field_has_no_bye(self):
        division = make_division(self.user, 6, 2)
        regenerate_pairings(division)
        byes = [
            p
            for p in division.pairings.filter(round=1)
            if p.first.player.is_bye or p.second.player.is_bye
        ]
        self.assertEqual(byes, [])

    def test_standings_exclude_the_bye(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        simulate_round(division, 1)

        pd = PairingData.for_division(division)
        standings = standings_after_round(pd, 1)
        self.assertEqual(len(standings), 5)  # all real players, no Bye
        self.assertFalse(any(p.is_bye for p in standings))


class ByeInResultsGridTests(TestCase):
    """The results grid holds bye rows, so it is the one grid that must see the
    bye entrant. It used to build its picker, its validation set and its
    portable payload from ``division.entrants`` — the manager that hides the
    bye — which made a division with an odd round unsavable."""

    def setUp(self):
        from tournaments.grids import ResultsGrid

        self.user = User.objects.create_user(username="g", password="p")
        self.division = make_division(self.user, 3, 2)
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        self.grid = ResultsGrid()
        self.bye_slip = self.division.result_slips.get(loser__player__is_bye=True)

    def rows(self):
        return [self.grid.serialize_row(s) for s in self.grid.queryset(self.division)]

    def test_the_bye_opponent_is_offered_in_the_picker(self):
        # Without this the Opponent cell holds a pk the picker cannot label, so
        # it renders blank and the row can never be saved.
        entrants = self.grid.lookups(self.division)["entrants"]
        self.assertIn(self.bye_slip.loser_id, [e["id"] for e in entrants])
        # It sorts last: the bye is not a competitor, and filing it among the
        # players invites picking it by accident.
        self.assertEqual(entrants[-1]["label"], "Bye")

    def test_a_bye_row_survives_an_untouched_save(self):
        rows = self.rows()
        (validated, errors) = self.grid.validate(rows, self.division)
        self.assertEqual(errors, [])
        prepared, errors = self.grid.prepare(self.division, validated)
        self.assertEqual(errors, [])
        self.grid.persist(self.division, prepared)
        self.assertTrue(
            self.division.result_slips.filter(loser__player__is_bye=True).exists()
        )

    def test_the_logged_payload_keeps_the_bye_opponent(self):
        # to_portable mapped the bye to None, so the recorded event described a
        # slip with no opponent — one replay could not rebuild.
        portable = self.grid.to_portable(self.rows(), self.division)
        bye_row = next(r for r in portable if r["winner_score"] == BYE_WINNER_SCORE)
        self.assertEqual(bye_row["loser"], "BYE")

    def test_a_payload_naming_the_bye_replays_into_a_fresh_division(self):
        portable = self.grid.to_portable(self.rows(), self.division)
        fresh = make_division(self.user, 3, 2, first_number=101)
        regenerate_pairings(fresh)
        publish_rounds(fresh, [1])
        rebuilt = self.grid.from_portable(portable, fresh)
        bye_row = next(r for r in rebuilt if r["winner_score"] == BYE_WINNER_SCORE)
        self.assertEqual(bye_row["loser"], fresh.bye_entrant().pk)

    def test_a_division_with_no_bye_is_offered_none(self):
        even = make_division(self.user, 4, 2, first_number=201)
        labels = [e["label"] for e in self.grid.lookups(even)["entrants"]]
        self.assertNotIn("Bye", labels)


class EditByeAndForfeitRowsTests(TestCase):
    """A bye or forfeit slip already in the table has to be *editable* there —
    it is a row like any other, and the director who wants to correct one has
    nowhere else to go."""

    def setUp(self):
        from tournaments.grids import ResultsGrid

        self.user = User.objects.create_user(username="e", password="p")
        self.division = make_division(self.user, 3, 2)
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        self.grid = ResultsGrid()
        self.bye_entrant = self.division.bye_entrant()

    def rows(self):
        return [self.grid.serialize_row(s) for s in self.grid.queryset(self.division)]

    def bye_row(self, rows):
        return next(r for r in rows if self.bye_entrant.pk in (r["winner"], r["loser"]))

    def save(self, rows):
        validated, errors = self.grid.validate(rows, self.division)
        if errors:
            return errors
        prepared, errors = self.grid.prepare(self.division, validated)
        if errors:
            return errors
        self.grid.persist(self.division, prepared)
        return []

    def test_a_bye_can_be_rescored(self):
        rows = self.rows()
        self.bye_row(rows)["winner_score"] = 0
        self.assertEqual(self.save(rows), [])
        slip = self.division.result_slips.get(loser__player__is_bye=True)
        self.assertEqual((slip.winner_score, slip.loser_score), (0, 0))

    def test_a_bye_row_can_be_deleted(self):
        rows = [r for r in self.rows() if self.bye_entrant.pk not in (r["winner"], r["loser"])]
        self.assertEqual(self.save(rows), [])
        self.assertFalse(
            self.division.result_slips.filter(loser__player__is_bye=True).exists()
        )

    def test_a_bye_cannot_be_reassigned_to_another_player(self):
        # Who got the bye is the printed board's business, not the result
        # table's. The grid *will* synthesize a pairing for a hand-entered bye
        # (phase 5), so what refuses this is the one-game-per-round rule: the
        # player named already has a game that round, and a second one would
        # leave the byed player's own row resultless and the round unable to
        # finish.
        rows = self.rows()
        row = self.bye_row(rows)
        other = next(
            e.pk for e in self.division.entrants.all() if e.pk != row["winner"]
        )
        row["winner"] = other
        self.assertTrue(self.save(rows))

    def test_the_started_column_is_derived_on_a_bye_row(self):
        # Not a free choice: winner_started orients the pairing the engine
        # replays into its start ledger, so ticking "Winner" here would charge
        # the byed player a start they never took, and correct_result_starts
        # skips bye pairings so nothing would put it back.
        rows = self.rows()
        self.bye_row(rows)["winner_started"] = True
        self.assertEqual(self.save(rows), [])
        slip = self.division.result_slips.get(loser__player__is_bye=True)
        self.assertFalse(slip.winner_started)

    def test_a_forfeit_row_round_trips_and_derives_its_start(self):
        """A forfeit is a bye with the sign flipped — the bye entrant wins 50-0.
        The grid has to carry it the same way, with the start derived the other
        way round (the bye is still the notional starter)."""
        rows = self.rows()
        row = self.bye_row(rows)
        row["winner"], row["loser"] = row["loser"], row["winner"]
        row["winner_score"], row["loser_score"] = BYE_WINNER_SCORE, 0
        row["winner_started"] = False
        self.assertEqual(self.save(rows), [])
        slip = self.division.result_slips.get(winner__player__is_bye=True)
        self.assertEqual((slip.winner_score, slip.loser_score), (BYE_WINNER_SCORE, 0))
        self.assertTrue(slip.winner_started)

    def test_a_forfeit_leaves_the_ledger_orientation_bye_first(self):
        from tournaments.pairing.base import PairingData, Pairings

        rows = self.rows()
        row = self.bye_row(rows)
        forfeiter = row["winner"]
        row["winner"], row["loser"] = row["loser"], row["winner"]
        self.assertEqual(self.save(rows), [])

        pairings = Pairings()
        for slip in PairingData.for_division(self.division).result_slips:
            pairings.add_result_slip(slip)
        bye_game = next(
            p for p in pairings if "BYE" in (p.first.key, p.second.key)
        )
        # The bye leads, so the real player is charged no start.
        self.assertEqual(bye_game.first.key, "BYE")
        self.assertEqual(
            bye_game.second.key,
            Entrant.all_objects.get(pk=forfeiter).player.player_number,
        )


class NotPlayedPredicateTests(TestCase):
    """``ResultSlipQuerySet.played`` and the sites that used to open-code it.

    Every one of those was written ``exclude(loser__player__is_bye=True)`` — the
    bye can only ever *win*. A forfeit slip puts the bye on the other side, so
    each mis-handled one (plans/PLAN_FORFEITS.md §6). These pin the fix by
    building the shape the old filters missed.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="n", password="p")
        self.division = make_division(self.user, 3, 2)
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        self.bye_slip = self.division.result_slips.get(loser__player__is_bye=True)

    def make_forfeit(self):
        """Turn the round's bye into a forfeit: the bye entrant wins 50–0."""
        slip = self.bye_slip
        slip.winner, slip.loser = slip.loser, slip.winner
        slip.winner_started = True
        slip.save()
        return slip

    def test_played_excludes_a_bye_on_either_side(self):
        self.assertNotIn(self.bye_slip, self.division.result_slips.played())
        self.make_forfeit()
        # The bye on *either* side is the whole test: a forfeit is a bye with
        # the winner the other way round, and a printed game is split rather
        # than annotated, so no forfeit is without one.
        self.assertNotIn(self.bye_slip, self.division.result_slips.played())

    def test_a_forfeit_does_not_make_a_published_round_in_progress(self):
        self.make_forfeit()
        rp = self.division.round_pairings_set.get(round=1)
        rp.update_status()
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)

    def test_a_forfeit_does_not_block_unpublishing(self):
        self.make_forfeit()
        self.assertEqual(unpublish_rounds(self.division, [1]), [1])
        # The derived slip goes with it, so the round is a clean draft again.
        self.assertFalse(self.division.result_slips.filter(round=1).exists())
        self.assertEqual(
            self.division.round_pairings_set.get(round=1).status, RoundPairings.DRAFT
        )

    def test_a_forfeit_stays_out_of_the_registry_export(self):
        self.make_forfeit()
        bundle = ExportTournament.from_db(self.division.tournament)
        results = [r for d in bundle.divisions for r in d.results]
        # The export keys players by number, so the bye shows up as "BYE".
        self.assertNotIn(
            BYE_PLAYER_NUMBER, {k for r in results for k in (r.winner, r.loser)}
        )
        self.assertEqual(results, [])

    def test_a_forfeit_stays_out_of_the_results_csv(self):
        # This export already tested both sides, so it is a guard on the
        # consolidation onto ``is_played`` rather than a fix. Asserting the real
        # game *is* there keeps it from passing vacuously.
        real = self.division.pairings.filter(round=1).exclude(
            second__player__is_bye=True
        ).first()
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=real,
            winner=real.first, winner_score=420,
            loser=real.second, loser_score=390, winner_started=True,
        )
        self.make_forfeit()
        self.division.tournament.editors.add(self.user)
        self.client.force_login(self.user)
        body = self.client.get(
            reverse("division_results_export", kwargs=self.division.slug_kwargs())
        ).content.decode()
        self.assertIn(real.first.name, body)
        self.assertNotIn("Bye", body)


class WithdrawalDoesNotStallPairingTests(TestCase):
    """Dropping a player used to stop the tournament dead.

    ``round_status`` in the Rust engine counted withdrawn players among those a
    round is waiting on. A withdrawn player has no result and never will, so
    every round after the withdrawal read Partial forever and ``can_pair``
    refused to pair the next one. The engine's own regression is
    ``a_withdrawn_player_does_not_stall_the_next_round``; this is the same bug
    seen from the app, which is where it actually bit.
    """

    def play(self, division, round_num):
        regenerate_pairings(division)
        rp = division.round_pairings_set.filter(round=round_num).first()
        if rp is None:
            return False
        publish_rounds(division, [round_num])
        for p in division.pairings.filter(round=round_num, result__isnull=True):
            ResultSlip.objects.create(
                division=division, round=round_num, pairing=p,
                winner=p.first, winner_score=420,
                loser=p.second, loser_score=380, winner_started=True,
            )
        division.round_pairings_set.get(round=round_num).update_status()
        return True

    def test_pairing_continues_after_a_withdrawal(self):
        user = User.objects.create_user(username="w", password="p")
        division = make_division(user, 6, 6)
        self.assertTrue(self.play(division, 1))
        division.entrants.filter(
            pk=division.entrants.order_by("number").first().pk
        ).update(dropped=True)
        division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()
        for round_num in (2, 3, 4):
            self.assertTrue(
                self.play(division, round_num), f"round {round_num} was not paired"
            )


class ByeRotationTests(TestCase):
    def test_no_player_gets_a_second_bye_until_all_have_one(self):
        user = User.objects.create_user(username="rot", password="p")
        for i in range(7):
            Player.objects.create(
                name=f"R{i}", player_number=f"R{i}", rating=1500 + i,
                is_provisional=False,
            )
        # 7 players, simulate 5 rounds (5 byes) — fewer byes than players.
        division = create_fake_tournament(user, num_players=7, num_rounds=6)

        bye_counts = Counter(
            slip.winner.name
            for slip in ResultSlip.objects.filter(
                division=division, loser__player__is_bye=True
            )
        )
        self.assertEqual(sum(bye_counts.values()), 5)
        self.assertTrue(all(count == 1 for count in bye_counts.values()))


class ByeExportTests(TestCase):
    def test_export_omits_the_bye_player_and_results(self):
        user = User.objects.create_user(username="exp", password="p")
        for i in range(5):
            Player.objects.create(
                name=f"E{i}", player_number=f"E{i}", rating=1500 + i,
                is_provisional=False,
            )
        division = create_fake_tournament(user, num_players=5, num_rounds=3)

        data = ExportTournament.from_db(division.tournament)
        self.assertNotIn("Bye", [p.name for p in data.players])
        exported = sum(len(d.results) for d in data.divisions)
        real = division.result_slips.exclude(loser__player__is_bye=True).count()
        self.assertEqual(exported, real)


class ByeIsNotSeededTests(TestCase):
    """The bye is not a competitor, so it takes no seed.

    It lives at number 0. Including it in the seeding handed it a number in the
    middle of the field and left the real entrants counting 1, 2, 3, 5.
    """

    def setUp(self):
        from datetime import date

        from users.models import User

        owner = User.objects.create_user(username="bye-seed", password="pw")
        self.tournament = Tournament.objects.create(
            name="Byes", location="X", start_date=date(2026, 5, 1), owner=owner,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        # A rated player and two unrated ones — the bye's own rating is 0, so
        # unrated entrants are who it can shuffle past.
        for i, (name, rating) in enumerate(
            [("Rated", 1500), ("Unrated", 0)], 1
        ):
            Entrant.enter(
                self.division,
                Player.objects.create(
                    name=name, player_number=f"000{i}", rating=rating
                ),
                i,
            )
        # A guest on a T- number: sorts *after* "BYE", so the bye displaces them.
        Entrant.enter(
            self.division,
            Player.objects.create(
                name="Guest", player_number="T-1", rating=0, is_provisional=True
            ),
            3,
        )
        self.bye = self.division.bye_entrant()

    def _numbers(self):
        return [
            (e.number, e.player.name)
            for e in self.division.entrants.order_by("number")
        ]

    def test_the_bye_takes_no_seed_and_leaves_no_gap(self):
        Entrant.apply_seeding(self.division, Entrant.seeding_for(self.division))
        self.bye.refresh_from_db()
        self.assertEqual(self.bye.number, 0)
        self.assertEqual(
            self._numbers(), [(1, "Rated"), (2, "Unrated"), (3, "Guest")]
        )

    def test_a_bye_already_holding_a_seat_is_sent_home(self):
        """Divisions seeded while it was included are repaired, not broken.

        The entrant taking that seat would otherwise collide with it on the
        unique (division, number).
        """
        # The state the bug left behind: the bye seeded into the middle of the
        # field, the guest it displaced pushed out to 4.
        guest = self.division.entrants.get(player__player_number="T-1")
        Entrant.all_objects.filter(pk=guest.pk).update(number=4)
        Entrant.all_objects.filter(pk=self.bye.pk).update(number=3)
        Entrant.apply_seeding(self.division, Entrant.seeding_for(self.division))
        self.bye.refresh_from_db()
        self.assertEqual(self.bye.number, 0)
        self.assertEqual(
            self._numbers(), [(1, "Rated"), (2, "Unrated"), (3, "Guest")]
        )

    def test_a_recorded_seeding_that_names_the_bye_still_replays(self):
        """Logs written while it was included must reproduce what happened."""
        Entrant.apply_seeding(
            self.division,
            [["0001", 1], ["0002", 2], ["BYE", 3], ["T-1", 4]],
        )
        self.bye.refresh_from_db()
        self.assertEqual(self.bye.number, 3)
        self.assertEqual(self._numbers(), [(1, "Rated"), (2, "Unrated"), (4, "Guest")])
