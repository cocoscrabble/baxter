from datetime import date

from django.test import TestCase

from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player as DBPlayer,
    ResultSlip,
    Tournament,
)
from tournaments.pairing.base import (
    PairingData,
    Player,
    RP,
    Repeats,
    RoundPairing,
    RoundStatus,
    Starts,
    round_status,
)
from tournaments.pairing.pair import can_pair, extract_pairings, pair
from users.models import User


# ── Repeats ──────────────────────────────────────────────


class RepeatsTests(TestCase):
    def setUp(self):
        self.repeats = Repeats()
        self.alice = Player("Alice")
        self.bob = Player("Bob")
        self.carol = Player("Carol")

    def test_get_unknown_pair_returns_zero(self):
        self.assertEqual(self.repeats.get(self.alice, self.bob), 0)

    def test_add_returns_count(self):
        self.assertEqual(self.repeats.add(self.alice, self.bob), 1)
        self.assertEqual(self.repeats.add(self.alice, self.bob), 2)

    def test_order_independent(self):
        self.repeats.add(self.alice, self.bob)
        self.assertEqual(self.repeats.get(self.bob, self.alice), 1)

    def test_distinct_pairs_tracked_separately(self):
        self.repeats.add(self.alice, self.bob)
        self.assertEqual(self.repeats.get(self.alice, self.carol), 0)


# ── Starts ───────────────────────────────────────────────


class StartsTests(TestCase):
    def setUp(self):
        self.starts = Starts()
        self.alice = Player("Alice")
        self.bob = Player("Bob")
        self.bye = Player("Bye")

    def test_register_records_starter(self):
        self.starts.register(self.alice, self.bob, 1)
        self.assertEqual(self.starts.starts["Alice"], 1)
        self.assertEqual(self.starts.starts["Bob"], 0)

    def test_add_fewer_starts_goes_first(self):
        self.starts.register(self.alice, self.bob, 1)
        # Alice has 1 start, Bob has 0 — Bob should start
        first, second = self.starts.add(self.alice, self.bob, 2)
        self.assertEqual(first.name, "Bob")
        self.assertEqual(second.name, "Alice")

    def test_add_equal_starts_alternates_h2h(self):
        # Alice started against Bob in round 1
        self.starts.register(self.alice, self.bob, 1)
        # Bob started against Alice in round 2 (equal starts now)
        self.starts.register(self.bob, self.alice, 2)
        # h2h[(Alice, Bob)] was set to False in round 2 (Bob started)
        # so not h2h[(Alice,Bob)] = True → Alice starts
        first, second = self.starts.add(self.alice, self.bob, 3)
        self.assertEqual(first.name, "Alice")
        self.assertEqual(second.name, "Bob")

    def test_add_bye_first(self):
        first, second = self.starts.add(self.bye, self.alice, 1)
        self.assertEqual(first.name, "Bye")

    def test_add_bye_second(self):
        first, second = self.starts.add(self.alice, self.bye, 1)
        self.assertEqual(first.name, "Bye")

    def test_fixed_starts(self):
        starts = Starts(fixed_starts={(1, "Bob"): True})
        first, second = starts.add(self.alice, self.bob, 1)
        self.assertEqual(first.name, "Bob")


# ── can_pair ─────────────────────────────────────────────


class CanPairTests(TestCase):
    def test_finished_round_cannot_pair(self):
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH.name)
        status = {1: RoundStatus.Finished}
        self.assertFalse(can_pair(rp, status))

    def test_start_round_zero_can_pair(self):
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH.name)
        status = {1: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))

    def test_start_round_finished_can_pair(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.KotH.name)
        status = {1: RoundStatus.Finished, 2: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))

    def test_start_round_not_finished_cannot_pair(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.KotH.name)
        status = {1: RoundStatus.Partial, 2: RoundStatus.Empty}
        self.assertFalse(can_pair(rp, status))

    def test_round_robin_ignores_start_round(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.RoundRobin.name)
        status = {1: RoundStatus.Empty, 2: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))


# ── DB-backed tests ──────────────────────────────────────


class PairingDBTestBase(TestCase):
    """Base class that sets up a 4-player division."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test", location="Test", start_date=date(2026, 1, 1), owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)
        cls.players = []
        cls.entrants = []
        for i, (name, rating) in enumerate(
            [("Alice", 1800), ("Bob", 1600), ("Carol", 1500), ("Dave", 1400)], start=1
        ):
            p = DBPlayer.objects.create(name=name, player_number=str(i).zfill(3), rating=rating)
            e = Entrant.objects.create(division=cls.division, player=p, number=i)
            cls.players.append(p)
            cls.entrants.append(e)

    def add_result(self, round, winner_idx, loser_idx, w_score, l_score, winner_started=True):
        return ResultSlip.objects.create(
            division=self.division,
            round=round,
            winner=self.entrants[winner_idx],
            winner_score=w_score,
            loser=self.entrants[loser_idx],
            loser_score=l_score,
            winner_started=winner_started,
        )


class RoundStatusTests(PairingDBTestBase):
    def test_empty_rounds(self):
        pd = PairingData.for_division(self.division)
        status = round_status(pd)
        # No results at all — defaultdict returns Empty for any round
        self.assertEqual(status[1], RoundStatus.Empty)

    def test_finished_round(self):
        self.add_result(1, 0, 1, 450, 380)
        self.add_result(1, 2, 3, 400, 350)
        pd = PairingData.for_division(self.division)
        status = round_status(pd)
        self.assertEqual(status[1], RoundStatus.Finished)

    def test_partial_round(self):
        self.add_result(1, 0, 1, 450, 380)
        # Only 1 of 2 games entered
        pd = PairingData.for_division(self.division)
        status = round_status(pd)
        self.assertEqual(status[1], RoundStatus.Partial)


class ExtractPairingsTests(PairingDBTestBase):
    def test_starter_first(self):
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        pd = PairingData.for_division(self.division)
        pairings = extract_pairings(pd, 1)
        self.assertEqual(len(pairings), 1)
        first, second = list(pairings)[0]
        self.assertEqual(first.name, "Alice")  # winner started
        self.assertEqual(second.name, "Bob")

    def test_loser_started(self):
        self.add_result(1, 0, 1, 450, 380, winner_started=False)
        pd = PairingData.for_division(self.division)
        pairings = extract_pairings(pd, 1)
        first, second = list(pairings)[0]
        self.assertEqual(first.name, "Bob")  # loser started
        self.assertEqual(second.name, "Alice")

    def test_filters_by_round(self):
        self.add_result(1, 0, 1, 450, 380)
        self.add_result(2, 0, 1, 450, 380)
        pd = PairingData.for_division(self.division)
        pairings = extract_pairings(pd, 1)
        self.assertEqual(len(pairings), 1)


class PairTests(PairingDBTestBase):
    def _koth_config(self, num_rounds):
        """Create KotH config where each round depends on the previous."""
        rp = []
        for i in range(1, num_rounds + 1):
            rp.append({"round": i, "pairing": RP.KotH.name, "start_round": i - 1})
        return DivisionSettings.objects.create(division=self.division, round_pairings=rp)

    def _pd(self):
        return PairingData.for_division(self.division)

    def test_first_round_pairs_by_seeding(self):
        settings = self._koth_config(1)
        result = pair(self._pd(), settings)
        # One round to pair
        self.assertEqual(len(result), 1)
        round_num, pairings = result[0]
        self.assertEqual(round_num, 1)
        self.assertEqual(len(pairings), 2)
        # KotH from seedings (by rating desc): Alice(1800)-Bob(1600), Carol(1500)-Dave(1400)
        names = [(p.first.name, p.second.name) for p in pairings]
        paired_sets = [set(n) for n in names]
        self.assertIn({"Alice", "Bob"}, paired_sets)
        self.assertIn({"Carol", "Dave"}, paired_sets)

    def test_finished_round_skipped_next_round_paired(self):
        settings = self._koth_config(2)
        # Round 1 complete: Alice beat Bob, Carol beat Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        result = pair(self._pd(), settings)
        # Round 1 is finished so only round 2 is returned
        self.assertEqual(len(result), 1)
        round_num, pairings = result[0]
        self.assertEqual(round_num, 2)
        # Standings: Alice(1 win, +70), Carol(1 win, +50), then Bob and Dave
        # KotH pairs 1v2, 3v4 from standings
        names = [(p.first.name, p.second.name) for p in pairings]
        paired_sets = [set(n) for n in names]
        self.assertIn({"Alice", "Carol"}, paired_sets)
        self.assertIn({"Bob", "Dave"}, paired_sets)

    def test_dependent_round_not_paired_if_start_round_incomplete(self):
        settings = self._koth_config(2)
        # Round 1 partial: only one result entered
        self.add_result(1, 0, 1, 450, 380)
        result = pair(self._pd(), settings)
        # Round 1 (start_round=0) can still be paired from seedings,
        # but round 2 depends on round 1 which is incomplete
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1)

    def test_repeats_tracked(self):
        settings = self._koth_config(3)
        # Round 1: Alice-Bob, Carol-Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # Round 2: Alice-Carol, Bob-Dave
        self.add_result(2, 0, 2, 430, 390, winner_started=True)
        self.add_result(2, 1, 3, 410, 370, winner_started=False)
        result = pair(self._pd(), settings)
        # Round 3 pairings should exist
        self.assertEqual(len(result), 1)
        round_num, pairings = result[0]
        self.assertEqual(round_num, 3)
        # Standings: Alice(2 wins), Bob(1 win), Carol(1 win), Dave(0 wins)
        # KotH pairs: Alice-Bob, Carol-Dave — repeats from round 1
        for p in pairings:
            names = {p.first.name, p.second.name}
            if names == {"Alice", "Bob"} or names == {"Carol", "Dave"}:
                self.assertEqual(p.repeats, 2)

    def test_all_rounds_finished_returns_empty(self):
        settings = self._koth_config(1)
        self.add_result(1, 0, 1, 450, 380)
        self.add_result(1, 2, 3, 400, 350)
        result = pair(self._pd(), settings)
        self.assertEqual(len(result), 0)

    def test_starts_balanced(self):
        settings = self._koth_config(3)
        # Round 1: Alice started vs Bob, Carol started vs Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # Round 2: Alice started vs Carol, Dave started vs Bob
        self.add_result(2, 0, 2, 430, 390, winner_started=True)
        self.add_result(2, 1, 3, 410, 370, winner_started=False)
        result = pair(self._pd(), settings)
        _, pairings = result[0]
        # After 2 rounds: Alice has 2 starts, Carol has 1, Dave has 1, Bob has 0.
        # Round 3 KotH: Alice-Bob, Carol-Dave.
        # Alice(2) vs Bob(0) → Bob starts. Carol(1) vs Dave(1) → Dave starts (h2h flip).
        # first in DisplayPairing is the starter.
        starters = {p.first.name for p in pairings}
        self.assertEqual(starters, {"Bob", "Dave"})
