"""Forfeits derived from a withdrawal (plans/PLAN_FORFEITS.md phase 2).

A withdrawn entrant whose absence is *recorded* keeps accruing results: every
round published while they are out gets a 0-50 loss against the bye entrant.
The rounds they forfeit are exactly the rounds published while they were
withdrawn, which is what makes rejoining need no backfill.
"""

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
