"""Forfeits derived from a withdrawal (plans/PLAN_FORFEITS.md phase 2).

A withdrawn entrant whose absence is *recorded* keeps accruing results: every
round published while they are out gets a 0-50 loss against the bye entrant.
The rounds they forfeit are exactly the rounds published while they were
withdrawn, which is what makes rejoining need no backfill.
"""

from django.db import models
from django.test import TestCase

from tournaments.generate_pairings import (
    BYE_WINNER_SCORE,
    publish_rounds,
    regenerate_pairings,
)
from tournaments.events import division_digest
from tournaments.live_ratings import project_ratings
from tournaments.models import (
    DivisionSettings,
    Entrant,
    ResultSlip,
    RoundPairings,
)
from tournaments.pairing.base import PairingData, standings_after_round
from tournaments.tests.test_byes import make_division
from tournaments.tournament_export import ExportTournament
from users.models import User


class DerivedForfeitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="f", password="p")
        # Even field, so nothing gets an ordinary bye and every bye-shaped row
        # in these tests is a forfeit.
        self.division = make_division(self.user, 6, 8)
        self.division.entrants.update(rating=1500, rating_source=Entrant.COCO)
        self.entrant = self.division.entrants.order_by("number").first()

    def play(self, round_num):
        """Publish a round and enter every real game, so the next one can pair."""
        regenerate_pairings(self.division)
        publish_rounds(self.division, [round_num])
        for p in self.division.pairings.filter(round=round_num, result__isnull=True):
            ResultSlip.objects.create(
                division=self.division, round=round_num, pairing=p,
                winner=p.first, winner_score=420,
                loser=p.second, loser_score=380, winner_started=True,
            )
        self.division.round_pairings_set.get(round=round_num).update_status()

    def set_settings(self, **fields):
        # Through the object, not a queryset update: ``division.settings`` is a
        # cached reverse relation, and the pairing code reads it off the same
        # division instance these tests hand it.
        settings = self.division.settings
        for name, value in fields.items():
            setattr(settings, name, value)
        settings.save(update_fields=list(fields))

    def set_policy(self, policy):
        self.set_settings(withdrawal=policy)

    def withdraw(self, *, forfeits=True):
        self.set_policy(
            DivisionSettings.FORFEIT if forfeits else DivisionSettings.OMIT
        )
        self.division.entrants.filter(pk=self.entrant.pk).update(dropped=True)
        self.division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()

    def rejoin(self):
        self.division.entrants.filter(pk=self.entrant.pk).update(dropped=False)
        self.division.round_pairings_set.filter(status=RoundPairings.DRAFT).delete()

    def pairing_for(self, entrant, round_num):
        from django.db.models import Q

        return self.division.pairings.filter(
            Q(first=entrant) | Q(second=entrant), round=round_num
        ).first()

    def forfeits_in(self, round_num):
        return self.division.result_slips.filter(
            round=round_num, winner__player__is_bye=True
        )

    def test_rounds_published_while_withdrawn_record_a_forfeit(self):
        for r in (1, 2):
            self.play(r)
        self.withdraw()
        for r in (3, 4):
            self.play(r)

        for r in (1, 2):
            self.assertFalse(self.forfeits_in(r).exists(), f"round {r}")
        for r in (3, 4):
            slip = self.forfeits_in(r).get()
            self.assertEqual(slip.loser_id, self.entrant.pk)
            self.assertEqual((slip.winner_score, slip.loser_score), (BYE_WINNER_SCORE, 0))
            self.assertTrue(slip.forfeit)
            # The bye leads, so the absent player is charged no start.
            self.assertTrue(slip.winner_started)

    def test_the_forfeits_show_as_losses_and_negative_spread(self):
        self.play(1)
        self.withdraw()
        for r in (2, 3):
            self.play(r)

        pd = PairingData.for_division(self.division)
        standing = next(
            p for p in standings_after_round(pd, 3, include_dropped=True)
            if p.key == self.entrant.key
        )
        self.assertEqual(standing.wins, 1)      # the round they played and won
        self.assertEqual(standing.losses, 2)    # the two forfeits
        self.assertEqual(standing.spread, 40 - 2 * BYE_WINNER_SCORE)

    def test_the_forfeits_are_not_rated_and_not_exported(self):
        self.play(1)
        self.withdraw()
        self.play(2)

        # The forfeit contributes nothing: only the game actually played is
        # counted, so the entrant is rated over one game, not two.
        projection = project_ratings(self.division)[self.entrant.key]
        self.assertEqual(projection.games, 1)

        results = [
            r for d in ExportTournament.from_db(self.division.tournament).divisions
            for r in d.results
        ]
        self.assertNotIn(
            self.entrant.key, [r.loser for r in results if r.winner == "BYE"]
        )
        self.assertTrue(all("BYE" not in (r.winner, r.loser) for r in results))

    def test_rejoining_keeps_the_forfeits_and_pairs_the_later_rounds(self):
        self.play(1)
        self.withdraw()
        for r in (2, 3):
            self.play(r)
        self.rejoin()
        for r in (4, 5):
            self.play(r)

        # The rounds missed keep the slips written when they were published...
        for r in (2, 3):
            self.assertTrue(self.forfeits_in(r).exists(), f"round {r}")
        # ...and nothing was backfilled for the rounds either side.
        for r in (1, 4, 5):
            self.assertFalse(self.forfeits_in(r).exists(), f"round {r}")
        # They are paired against a real opponent again.
        for r in (4, 5):
            pairing = self.pairing_for(self.entrant, r)
            self.assertIsNotNone(pairing, f"round {r}")
            self.assertFalse(pairing.forfeit)
            self.assertFalse(
                pairing.first.player.is_bye or pairing.second.player.is_bye
            )

    def test_the_spread_comes_from_the_division_not_a_constant(self):
        # A WESPA event scores byes and forfeits at 100, not the NSA 50.
        self.set_settings(bye_spread=100)
        self.withdraw()
        self.play(1)
        slip = self.forfeits_in(1).get()
        self.assertEqual(slip.winner_score, 100)
        bye = self.division.result_slips.get(loser__player__is_bye=True)
        self.assertEqual(bye.winner_score, 100)

    def test_withdrawing_without_forfeits_records_nothing(self):
        self.play(1)
        self.withdraw(forfeits=False)
        for r in (2, 3):
            self.play(r)
        for r in (2, 3):
            self.assertFalse(self.forfeits_in(r).exists(), f"round {r}")
            self.assertFalse(
                self.division.pairings.filter(round=r, forfeit=True).exists()
            )

    def test_regenerating_does_not_duplicate_the_forfeit_row(self):
        self.withdraw()
        for _ in range(3):
            regenerate_pairings(self.division)
        self.assertEqual(
            self.division.pairings.filter(round=1, forfeit=True).count(), 1
        )
        # And the digest is unchanged by re-deriving, which is what lets replay
        # regenerate before every publish without moving the recorded state.
        publish_rounds(self.division, [1])
        before = division_digest(self.division)
        regenerate_pairings(self.division)
        self.assertEqual(division_digest(self.division), before)

    def test_a_bye_and_a_forfeit_coexist_in_one_round(self):
        # Six entrants, one withdrawn, leaves an odd field of five: somebody
        # gets a real bye *and* the withdrawn player gets a forfeit. The engine
        # never saw the withdrawn entrant, so adding the forfeit row on top
        # cannot disturb the parity the bye was computed from.
        self.withdraw()
        self.play(1)
        rows = self.division.pairings.filter(round=1)
        self.assertEqual(rows.filter(forfeit=True).count(), 1)
        byes = [
            p for p in rows
            if not p.forfeit and (p.first.player.is_bye or p.second.player.is_bye)
        ]
        self.assertEqual(len(byes), 1)
        self.assertEqual(rows.count(), 4)  # 2 real games + 1 bye + 1 forfeit

    def test_a_forfeit_round_still_reaches_finished(self):
        self.withdraw()
        self.play(1)
        self.assertEqual(
            self.division.round_pairings_set.get(round=1).status,
            RoundPairings.FINISHED,
        )


class HandEnteredAbsenceTests(TestCase):
    """Entering a bye or a forfeit in the edit-results grid (phase 5).

    Issue #57 asks for "a way to enter both forfeits and byes in the edit
    results workflow without hitting the 'all fields should be filled out'
    validation". Two things were in the way: the blank scores, and the fact
    that a hand-entered absence has no pairing to hang on.
    """

    def setUp(self):
        from tournaments.grids import ResultsGrid

        self.user = User.objects.create_user(username="h", password="p")
        self.division = make_division(self.user, 6, 4, first_number=401)
        self.division.entrants.update(rating=1500, rating_source=Entrant.COCO)
        self.grid = ResultsGrid()
        self.bye = self.division.bye_entrant()
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        # A round published without one entrant, the way a withdrawal leaves it:
        # they have no game, so a hand-entered absence has nothing to resolve to.
        self.absentee = self.division.entrants.order_by("number").first()
        self.division.pairings.filter(round=1).filter(
            models.Q(first=self.absentee) | models.Q(second=self.absentee)
        ).delete()

    def rows(self):
        return [self.grid.serialize_row(s) for s in self.grid.queryset(self.division)]

    def save(self, rows):
        validated, errors = self.grid.validate(rows, self.division)
        if errors:
            return errors
        prepared, errors = self.grid.prepare(self.division, validated)
        if errors:
            return errors
        self.grid.persist(self.division, prepared)
        self.grid.after_save(self.division)
        return []

    def absence_row(self, *, forfeit, **overrides):
        """A grid row for the absentee, scores left blank as a director would."""
        row = {
            "round": 1,
            "winner": self.bye.pk if forfeit else self.absentee.pk,
            "winner_score": None,
            "loser": self.absentee.pk if forfeit else self.bye.pk,
            "loser_score": None,
            "winner_started": None,
        }
        return {**row, **overrides}

    def test_a_bye_can_be_entered_with_no_scores(self):
        self.assertEqual(self.save(self.rows() + [self.absence_row(forfeit=False)]), [])
        slip = self.division.result_slips.get(winner=self.absentee)
        self.assertEqual((slip.winner_score, slip.loser_score), (50, 0))
        self.assertFalse(slip.winner_started)
        pairing = slip.pairing
        self.assertIsNotNone(pairing)
        self.assertFalse(pairing.forfeit)
        self.assertTrue(pairing.second.player.is_bye)

    def test_a_forfeit_can_be_entered_with_no_scores(self):
        self.assertEqual(self.save(self.rows() + [self.absence_row(forfeit=True)]), [])
        slip = self.division.result_slips.get(loser=self.absentee)
        self.assertEqual((slip.winner_score, slip.loser_score), (50, 0))
        # The bye leads, so the absentee is charged no start.
        self.assertTrue(slip.winner_started)
        self.assertTrue(slip.pairing.forfeit)

    def test_the_round_reaches_finished(self):
        self.assertEqual(self.save(self.rows() + [self.absence_row(forfeit=True)]), [])
        for p in self.division.pairings.filter(round=1, result__isnull=True):
            ResultSlip.objects.create(
                division=self.division, round=1, pairing=p,
                winner=p.first, winner_score=420,
                loser=p.second, loser_score=380, winner_started=True,
            )
        self.division.round_pairings_set.get(round=1).update_status()
        self.assertEqual(
            self.division.round_pairings_set.get(round=1).status,
            RoundPairings.FINISHED,
        )

    def test_a_typed_spread_is_honoured(self):
        # TSH lets a one-off forfeit be scored differently from the tournament
        # default; only a *blank* cell is filled in.
        self.assertEqual(
            self.save(self.rows() + [self.absence_row(forfeit=True, winner_score=75)]),
            [],
        )
        slip = self.division.result_slips.get(loser=self.absentee)
        self.assertEqual(slip.winner_score, 75)

    def test_the_division_spread_is_used_not_a_constant(self):
        settings = self.division.settings
        settings.bye_spread = 100
        settings.save(update_fields=["bye_spread"])
        self.assertEqual(self.save(self.rows() + [self.absence_row(forfeit=False)]), [])
        self.assertEqual(
            self.division.result_slips.get(winner=self.absentee).winner_score, 100
        )

    def test_a_player_who_already_has_a_game_is_refused(self):
        playing = self.division.pairings.filter(round=1).exclude(
            second__player__is_bye=True
        ).first().first
        errors = self.save(
            self.rows()
            + [self.absence_row(forfeit=True, loser=playing.pk, winner=self.bye.pk)]
        )
        self.assertTrue(errors)
        self.assertIn("already has a game", errors[0])

    def test_an_unpaired_round_is_refused_clearly(self):
        errors = self.save(
            self.rows() + [self.absence_row(forfeit=True, round=4)]
        )
        self.assertTrue(errors)
        self.assertIn("no pairings yet", errors[0])

    def test_nothing_is_written_when_a_later_row_fails(self):
        # prepare() runs before the save transaction, which is why the pairing
        # is built in persist(): a pairing created during validation would
        # outlive the error.
        before = self.division.pairings.filter(round=1).count()
        errors = self.save(
            self.rows()
            + [self.absence_row(forfeit=True)]
            + [self.absence_row(forfeit=True, round=4)]
        )
        self.assertTrue(errors)
        self.assertEqual(self.division.pairings.filter(round=1).count(), before)


class HandEnteredAbsenceReplayTests(TestCase):
    """A hand-entered absence lands a *pairing* in a published round, which
    ``division_digest`` covers — so a replay has to rebuild it from the logged
    rows alone, synthesized pairing and all."""

    def test_the_grid_save_replays_to_the_same_digest(self):
        import json

        from django.urls import reverse

        from tournaments.commands import (
            add_entrant, create_division, create_tournament, publish_round,
            save_settings,
        )
        from tournaments.forfeits import withdraw_entrant
        from tournaments.events import division_digest, export_jsonl
        from tournaments.grids import ResultsGrid
        from tournaments.models import Player, Tournament
        from tournaments.replay import parse_jsonl, replay

        user = User.objects.create_user(username="hr", password="p")
        tournament = create_tournament(None, user, {
            "name": "Grid Replay", "location": "x", "start_date": "2026-03-15",
        })
        create_division(tournament, user, {"name": "D"})
        for i in range(1, 7):
            Player.objects.create(name=f"G{i}", player_number=f"8{i:02d}",
                                  rating=1600 - i)
            add_entrant(tournament, user, {"division": "D", "player": f"8{i:02d}"})
        save_settings(tournament, user, {
            "division": "D",
            "blocks": [{"pairing": "KotH", "rounds": 3, "pair_from": 1}],
        })
        division = tournament.divisions.get(name="D")
        regenerate_pairings(division)
        publish_round(tournament, user, {"division": "D", "round": 1})

        # Leave one entrant unpaired in the published round the way the app
        # really does: a withdrawal under OMIT dissolves their printed game,
        # gives the opponent a bye, and records nothing for them. Deleting a
        # pairing by hand would leave a state no replay could reach.
        absentee = division.entrants.order_by("number").first()
        withdraw_entrant(tournament, user, {"division": "D", "player": absentee.key})
        self.assertFalse(
            division.pairings.filter(round=1).filter(
                models.Q(first=absentee) | models.Q(second=absentee)
            ).exists()
        )

        grid = ResultsGrid()
        rows = [grid.serialize_row(s) for s in grid.queryset(division)]
        rows.append({
            "round": 1, "winner": division.bye_entrant().pk,
            "winner_score": None, "loser": absentee.pk, "loser_score": None,
            "winner_started": None,
        })
        self.client.force_login(user)
        response = self.client.post(
            reverse("division_edit_results", kwargs=division.slug_kwargs()),
            json.dumps({"rows": rows}), content_type="application/json",
        )
        self.assertTrue(response.json().get("ok"), response.json())
        self.assertEqual(division.pairings.filter(round=1, forfeit=True).count(), 1)
        expected = division_digest(division)

        jsonl = export_jsonl(tournament)
        Tournament.objects.all().delete()
        _header, events = parse_jsonl(jsonl)
        rebuilt = replay(events, verify=True).tournament.divisions.get(name="D")
        self.assertEqual(division_digest(rebuilt), expected)
        forfeit = rebuilt.pairings.get(round=1, forfeit=True)
        self.assertEqual(forfeit.first.player.player_number, absentee.key)
        self.assertEqual(forfeit.result.winner_score, 50)
