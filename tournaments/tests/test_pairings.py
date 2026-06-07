from datetime import date

from django.test import TestCase

from django.db import IntegrityError

from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    FixedPairing as DBFixedPairing,
    FixedTable as DBFixedTable,
    Pairing as DBPairing,
    Player as DBPlayer,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.pairing.base import (
    EntrantData,
    Pairing,
    PairingData,
    Player,
    PlayerData,
    Repeats,
    ResultSlipData,
    RoundStatus,
    Starts,
    standings_after_round,
)
from tournaments.pairing.round_pairing import RP, RoundPairing
from tournaments.pairing.pair import can_pair, extract_pairings, pair, pair_round, round_status
from users.models import User


# ── Repeats ──────────────────────────────────────────────


class RepeatsTests(TestCase):
    def setUp(self):
        self.repeats = Repeats()
        self.alice = Player("Alice")
        self.bob = Player("Bob")
        self.carol = Player("Carol")

    def test_get_unknown_pair_returns_zero(self):
        self.assertEqual(self.repeats.get(Pairing(self.alice, self.bob)), 0)

    def test_add_returns_count(self):
        self.assertEqual(self.repeats.add(Pairing(self.alice, self.bob)), 1)
        self.assertEqual(self.repeats.add(Pairing(self.alice, self.bob)), 2)

    def test_order_independent(self):
        self.repeats.add(Pairing(self.alice, self.bob))
        self.assertEqual(self.repeats.get(Pairing(self.bob, self.alice)), 1)

    def test_distinct_pairs_tracked_separately(self):
        self.repeats.add(Pairing(self.alice, self.bob))
        self.assertEqual(self.repeats.get(Pairing(self.alice, self.carol)), 0)


# ── Starts ───────────────────────────────────────────────


class StartsTests(TestCase):
    def setUp(self):
        self.starts = Starts()
        self.alice = Player("Alice")
        self.bob = Player("Bob")
        self.bye = Player("Bye")

    def test_register_records_starter(self):
        self.starts.register(Pairing(self.alice, self.bob), 1)
        self.assertEqual(self.starts.starts["Alice"], 1)
        self.assertEqual(self.starts.starts["Bob"], 0)

    def test_add_fewer_starts_goes_first(self):
        self.starts.register(Pairing(self.alice, self.bob), 1)
        # Alice has 1 start, Bob has 0 — Bob should start
        p = self.starts.add(Pairing(self.alice, self.bob), 2)
        self.assertEqual(p.first.name, "Bob")
        self.assertEqual(p.second.name, "Alice")

    def test_add_equal_starts_alternates_h2h(self):
        # Alice started against Bob in round 1
        self.starts.register(Pairing(self.alice, self.bob), 1)
        # Bob started against Alice in round 2 (equal starts now)
        self.starts.register(Pairing(self.bob, self.alice), 2)
        # h2h[(Alice, Bob)] was set to False in round 2 (Bob started)
        # so not h2h[(Alice,Bob)] = True → Alice starts
        p = self.starts.add(Pairing(self.alice, self.bob), 3)
        self.assertEqual(p.first.name, "Alice")
        self.assertEqual(p.second.name, "Bob")

    def test_add_bye_first(self):
        p = self.starts.add(Pairing(self.bye, self.alice), 1)
        self.assertEqual(p.first.name, "Bye")

    def test_add_bye_second(self):
        p = self.starts.add(Pairing(self.alice, self.bye), 1)
        self.assertEqual(p.first.name, "Bye")

    def test_fixed_starts(self):
        starts = Starts(fixed_starts={(1, "Bob"): True})
        p = starts.add(Pairing(self.alice, self.bob), 1)
        self.assertEqual(p.first.name, "Bob")


# ── can_pair ─────────────────────────────────────────────


class CanPairTests(TestCase):
    def test_finished_round_cannot_pair(self):
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        status = {1: RoundStatus.Finished}
        self.assertFalse(can_pair(rp, status))

    def test_start_round_zero_can_pair(self):
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        status = {1: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))

    def test_start_round_finished_can_pair(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.KotH)
        status = {1: RoundStatus.Finished, 2: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))

    def test_start_round_not_finished_cannot_pair(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.KotH)
        status = {1: RoundStatus.Partial, 2: RoundStatus.Empty}
        self.assertFalse(can_pair(rp, status))

    def test_partial_round_cannot_pair(self):
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        status = {1: RoundStatus.Partial}
        self.assertFalse(can_pair(rp, status))

    def test_partial_round_robin_cannot_pair(self):
        rp = RoundPairing(round=1, start_round=1, pairing=RP.RoundRobin)
        status = {1: RoundStatus.Partial}
        self.assertFalse(can_pair(rp, status))

    def test_round_robin_ignores_start_round(self):
        rp = RoundPairing(round=2, start_round=1, pairing=RP.RoundRobin)
        status = {1: RoundStatus.Empty, 2: RoundStatus.Empty}
        self.assertTrue(can_pair(rp, status))


# ── DB-backed tests ──────────────────────────────────────


class PairingDBTestBase(TestCase):
    """Base class that sets up a 4-player division."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test",
            location="Test",
            start_date=date(2026, 1, 1),
            owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)
        cls.players = []
        cls.entrants = []
        for i, (name, rating) in enumerate(
            [("Alice", 1800), ("Bob", 1600), ("Carol", 1500), ("Dave", 1400)], start=1
        ):
            p = DBPlayer.objects.create(
                name=name, player_number=str(i).zfill(3), rating=rating
            )
            e = Entrant.objects.create(division=cls.division, player=p, number=i)
            cls.players.append(p)
            cls.entrants.append(e)

    def add_result(
        self, round, winner_idx, loser_idx, w_score, l_score, winner_started=True
    ):
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
        p = list(pairings)[0]
        self.assertEqual(p.first.name, "Alice")  # winner started
        self.assertEqual(p.second.name, "Bob")

    def test_loser_started(self):
        self.add_result(1, 0, 1, 450, 380, winner_started=False)
        pd = PairingData.for_division(self.division)
        pairings = extract_pairings(pd, 1)
        p = list(pairings)[0]
        self.assertEqual(p.first.name, "Bob")  # loser started
        self.assertEqual(p.second.name, "Alice")

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
            rp.append({"round": i, "pairing": RP.KotH, "start_round": i - 1})
        return DivisionSettings.objects.create(
            division=self.division, round_pairings=rp
        )

    def _pd(self):
        return PairingData.for_division(self.division)

    def test_first_round_pairs_by_seeding(self):
        self._koth_config(1)
        result = pair(self._pd())
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
        self._koth_config(2)
        # Round 1 complete: Alice beat Bob, Carol beat Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        result = pair(self._pd())
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

    def test_no_round_paired_if_any_round_is_partial(self):
        self._koth_config(2)
        # Round 1 partial: only one result entered
        self.add_result(1, 0, 1, 450, 380)
        result = pair(self._pd())
        # Round 1 is partial so it cannot be re-paired; round 2 also blocked
        self.assertEqual(len(result), 0)

    def test_repeats_tracked(self):
        self._koth_config(3)
        # Round 1: Alice-Bob, Carol-Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # Round 2: Alice-Carol, Bob-Dave
        self.add_result(2, 0, 2, 430, 390, winner_started=True)
        self.add_result(2, 1, 3, 410, 370, winner_started=False)
        result = pair(self._pd())
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
        self._koth_config(1)
        self.add_result(1, 0, 1, 450, 380)
        self.add_result(1, 2, 3, 400, 350)
        result = pair(self._pd())
        self.assertEqual(len(result), 0)

    def test_starts_balanced(self):
        self._koth_config(3)
        # Round 1: Alice started vs Bob, Carol started vs Dave
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # Round 2: Alice started vs Carol, Dave started vs Bob
        self.add_result(2, 0, 2, 430, 390, winner_started=True)
        self.add_result(2, 1, 3, 410, 370, winner_started=False)
        result = pair(self._pd())
        _, pairings = result[0]
        # After 2 rounds: Alice has 2 starts, Carol has 1, Dave has 1, Bob has 0.
        # Round 3 KotH: Alice-Bob, Carol-Dave.
        # Alice(2) vs Bob(0) → Bob starts. Carol(1) vs Dave(1) → Dave starts (h2h flip).
        # first in DisplayPairing is the starter.
        starters = {p.first.name for p in pairings}
        self.assertEqual(starters, {"Bob", "Dave"})


# ── standings_after_round with excluded_names ────────────────────────────────


def _make_pd(names, result_slips=None, fixed_pairings=None):
    """Build a PairingData from a list of player names (first = highest rated)."""
    n = len(names)
    entrants = [
        EntrantData(PlayerData(name=name, rating=(n - i) * 100))
        for i, name in enumerate(names)
    ]
    return PairingData(
        result_slips=result_slips or [],
        entrants=entrants,
        repeats=Repeats(),
        fixed_pairings=fixed_pairings or {},
    )


class StandingsExclusionTests(TestCase):
    """standings_after_round filters pd.excluded_names from its output."""

    def test_filters_excluded_from_seedings(self):
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"])
        pd.excluded_names = {"Alice", "Dave"}
        names = [p.name for p in standings_after_round(pd, 0)]
        self.assertNotIn("Alice", names)
        self.assertNotIn("Dave", names)
        self.assertIn("Bob", names)
        self.assertIn("Carol", names)

    def test_filters_excluded_from_results_standings(self):
        slips = [
            ResultSlipData(1, "Alice", "Bob", 400, 350, True),
            ResultSlipData(1, "Carol", "Dave", 420, 360, True),
        ]
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"], result_slips=slips)
        pd.excluded_names = {"Alice", "Carol"}
        names = [p.name for p in standings_after_round(pd, 1)]
        self.assertNotIn("Alice", names)
        self.assertNotIn("Carol", names)
        self.assertIn("Bob", names)
        self.assertIn("Dave", names)

    def test_empty_excluded_names_returns_all(self):
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"])
        self.assertEqual(len(standings_after_round(pd, 0)), 4)


# ── pair_round with fixed pairings ───────────────────────────────────────────


class PairRoundFixedTests(TestCase):
    """pair_round injects fixed pairings and excludes those players from the strategy."""

    def _pair_sets(self, pairings):
        return [{p.first.name, p.second.name} for p in pairings]

    def test_fixed_pair_included_in_output(self):
        # Fix Alice-Dave; KotH on remaining Bob-Carol.
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"], fixed_pairings={1: [("Alice", "Dave")]})
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        self.assertIn({"Alice", "Dave"}, self._pair_sets(pair_round(pd, rp)))

    def test_strategy_only_sees_remaining_players(self):
        # With Alice-Dave fixed, KotH gets [Bob, Carol] and must pair them together.
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"], fixed_pairings={1: [("Alice", "Dave")]})
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        pairings = list(pair_round(pd, rp))
        self.assertEqual(len(pairings), 2)
        self.assertIn({"Bob", "Carol"}, self._pair_sets(pairings))

    def test_fixed_players_not_double_paired(self):
        # Alice and Dave must not appear in any strategy-generated pair.
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"], fixed_pairings={1: [("Alice", "Dave")]})
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        pair_sets = self._pair_sets(pair_round(pd, rp))
        for s in pair_sets:
            if s != {"Alice", "Dave"}:
                self.assertNotIn("Alice", s)
                self.assertNotIn("Dave", s)

    def test_excluded_names_cleared_after_call(self):
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"], fixed_pairings={1: [("Alice", "Dave")]})
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        pair_round(pd, rp)
        self.assertEqual(pd.excluded_names, set())

    def test_no_fixed_pairings_behaves_normally(self):
        # Sanity check: without fixed pairings, KotH pairs by seeding order.
        pd = _make_pd(["Alice", "Bob", "Carol", "Dave"])
        rp = RoundPairing(round=1, start_round=0, pairing=RP.KotH)
        pair_sets = self._pair_sets(pair_round(pd, rp))
        self.assertIn({"Alice", "Bob"}, pair_sets)
        self.assertIn({"Carol", "Dave"}, pair_sets)


# ── DB-backed integration tests ──────────────────────────────────────────────


class FixedPairingIntegrationTests(PairingDBTestBase):
    """End-to-end tests for fixed pairings through the full pair() pipeline."""

    def _koth_config(self, num_rounds):
        rp = [{"round": i, "pairing": RP.KotH, "start_round": i - 1} for i in range(1, num_rounds + 1)]
        return DivisionSettings.objects.create(division=self.division, round_pairings=rp)

    def _add_fixed(self, round_number, idx1, idx2):
        return DBFixedPairing.objects.create(
            division=self.division,
            round_number=round_number,
            entrant1=self.entrants[idx1],
            entrant2=self.entrants[idx2],
        )

    def _pd(self):
        return PairingData.for_division(self.division)

    def test_for_division_loads_fixed_pairings(self):
        self._add_fixed(1, 0, 3)  # Alice-Dave in round 1
        pd = self._pd()
        self.assertIn(1, pd.fixed_pairings)
        self.assertEqual(set(pd.fixed_pairings[1][0]), {"Alice", "Dave"})

    def test_for_division_no_fixed_pairings_is_empty(self):
        pd = self._pd()
        self.assertEqual(pd.fixed_pairings, {})

    def test_for_division_multiple_fixed_in_same_round(self):
        # All four players fixed in round 1: Alice-Dave and Bob-Carol.
        self._add_fixed(1, 0, 3)
        self._add_fixed(1, 1, 2)
        pd = self._pd()
        self.assertEqual(len(pd.fixed_pairings[1]), 2)
        pair_sets = [set(pair) for pair in pd.fixed_pairings[1]]
        self.assertIn({"Alice", "Dave"}, pair_sets)
        self.assertIn({"Bob", "Carol"}, pair_sets)

    def test_fixed_pair_appears_in_pair_output(self):
        self._add_fixed(1, 0, 3)  # Alice-Dave fixed
        self._koth_config(1)
        _, pairings = pair(self._pd())[0]
        pair_sets = [{p.first.name, p.second.name} for p in pairings]
        self.assertIn({"Alice", "Dave"}, pair_sets)

    def test_non_fixed_players_paired_by_strategy(self):
        # With Alice-Dave fixed, KotH pairs the two remaining players Bob and Carol.
        self._add_fixed(1, 0, 3)
        self._koth_config(1)
        _, pairings = pair(self._pd())[0]
        pair_sets = [{p.first.name, p.second.name} for p in pairings]
        self.assertIn({"Bob", "Carol"}, pair_sets)

    def test_fixed_pair_goes_through_starts_balancing(self):
        # Round 1: Alice starts vs Bob (Alice wins), Carol starts vs Dave.
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # After round 1: Alice has 1 start, Bob has 0.
        # Round 2: fix Alice-Bob. Starts balancing should give Bob the start.
        self._add_fixed(2, 0, 1)
        self._koth_config(2)
        _, pairings = pair(self._pd())[0]
        alice_bob = next(p for p in pairings if {p.first.name, p.second.name} == {"Alice", "Bob"})
        self.assertEqual(alice_bob.first.name, "Bob")

    def test_fixed_pair_repeats_tracked(self):
        # Round 1: Alice beats Bob — their first meeting.
        self.add_result(1, 0, 1, 450, 380, winner_started=True)
        self.add_result(1, 2, 3, 400, 350, winner_started=True)
        # Round 2: fix Alice-Bob again. repeats should be 2 (once in R1, once in R2).
        self._add_fixed(2, 0, 1)
        self._koth_config(2)
        _, pairings = pair(self._pd())[0]
        alice_bob = next(p for p in pairings if {p.first.name, p.second.name} == {"Alice", "Bob"})
        self.assertEqual(alice_bob.repeats, 2)


# ── _regenerate_pairings with fixed tables ────────────────────────────────────


class FixedTableIntegrationTests(PairingDBTestBase):
    """Tests for fixed table assignment in _regenerate_pairings.

    Players by rating: Alice(1), Bob(2), Carol(3), Dave(4).
    KotH round 1 pairs: Alice-Bob (table 1), Carol-Dave (table 2) without fixed tables.
    """

    def _koth_config(self, num_rounds):
        rp = [{"round": i, "pairing": RP.KotH, "start_round": i - 1} for i in range(1, num_rounds + 1)]
        return DivisionSettings.objects.create(division=self.division, round_pairings=rp)

    def _add_fixed_table(self, round_number, entrant_idx, table_number):
        return DBFixedTable.objects.create(
            division=self.division,
            round_number=round_number,
            entrant=self.entrants[entrant_idx],
            table_number=table_number,
        )

    def _regenerate(self):
        from tournaments.generate_pairings import regenerate_pairings
        regenerate_pairings(self.division)
        return list(
            DBPairing.objects.filter(division=self.division)
            .select_related("first__player", "second__player")
            .order_by("round", "table")
        )

    def _table_for(self, pairings, name1, name2):
        for p in pairings:
            if {p.first.player.name, p.second.player.name} == {name1, name2}:
                return p.table
        return None

    def test_no_fixed_tables_assigns_tables_1_to_n(self):
        self._koth_config(1)
        pairings = self._regenerate()
        self.assertEqual(len(pairings), 2)
        tables = {p.table for p in pairings}
        self.assertEqual(tables, {1, 2})

    def test_higher_standing_pair_gets_lower_table(self):
        # Alice-Bob (ranks 1,2) should be at table 1; Carol-Dave (ranks 3,4) at table 2.
        self._koth_config(1)
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 1)
        self.assertEqual(self._table_for(pairings, "Carol", "Dave"), 2)

    def test_fixed_table_assigned_to_pairing(self):
        self._koth_config(1)
        self._add_fixed_table(1, 0, 2)  # Alice → table 2
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 2)

    def test_all_sentinel_applies_to_round(self):
        # Bob fixed to table 1 for "all" rounds (-1); should get table 1 in round 1.
        self._koth_config(1)
        self._add_fixed_table(-1, 1, 1)  # Bob → table 1 (all rounds)
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 1)

    def test_round_specific_overrides_all(self):
        # Bob has all→table 1, but round 1 specific→table 2. Specific wins.
        self._koth_config(1)
        self._add_fixed_table(-1, 1, 1)  # Bob → table 1 (all)
        self._add_fixed_table(1, 1, 2)   # Bob → table 2 (round 1 specific)
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 2)

    def test_conflict_both_specific_higher_standing_wins(self):
        # Alice (rank 1) fixed → table 1; Bob (rank 2) fixed → table 2.
        # Both specific; Alice has higher standing → her table (1) is used.
        self._koth_config(1)
        self._add_fixed_table(1, 0, 1)  # Alice → table 1
        self._add_fixed_table(1, 1, 2)  # Bob → table 2
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 1)

    def test_conflict_specific_beats_all(self):
        # Alice has all→table 2; Bob has specific→table 1. Specific wins.
        self._koth_config(1)
        self._add_fixed_table(-1, 0, 2)  # Alice → table 2 (all)
        self._add_fixed_table(1, 1, 1)   # Bob → table 1 (specific)
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Alice", "Bob"), 1)

    def test_free_pairings_fill_remaining_slots(self):
        # Alice fixed to table 2. Carol-Dave (free) should fill table 1.
        self._koth_config(1)
        self._add_fixed_table(1, 0, 2)  # Alice → table 2
        pairings = self._regenerate()
        self.assertEqual(self._table_for(pairings, "Carol", "Dave"), 1)


# ── RoundPairings lifecycle ─────────────────────────────


class RoundPairingsLifecycleTests(PairingDBTestBase):
    """Tests for the RoundPairings model and lifecycle transitions."""

    def _koth_config(self, num_rounds):
        rp = []
        for i in range(1, num_rounds + 1):
            rp.append({"round": i, "pairing": RP.KotH, "start_round": i - 1})
        return DivisionSettings.objects.create(
            division=self.division, round_pairings=rp
        )

    def _regenerate(self):
        from tournaments.generate_pairings import regenerate_pairings
        regenerate_pairings(self.division)

    def _complete_round(self, round_num):
        """Add results for all pairings in a round so the next round can be paired."""
        self.add_result(round_num, 0, 1, 450, 380)
        self.add_result(round_num, 2, 3, 400, 350)

    # -- Steps 1-3: model structure --

    def test_round_pairings_created_on_generate(self):
        self._koth_config(1)
        self._regenerate()
        rps = RoundPairings.objects.filter(division=self.division).order_by("round")
        self.assertEqual(rps.count(), 1)
        rp = rps.first()
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        self.assertEqual(rp.pairings.count(), 2)  # 4 players = 2 pairings
        # Verify FK is set on Pairing objects
        for p in DBPairing.objects.filter(division=self.division):
            self.assertIsNotNone(p.round_pairings)

    def test_round_pairings_multiple_rounds(self):
        """With results for round 1, regenerate creates RoundPairings for rounds 1 and 2."""
        self._koth_config(2)
        self._complete_round(1)
        self._regenerate()
        rps = RoundPairings.objects.filter(division=self.division).order_by("round")
        self.assertEqual(rps.count(), 1)  # Only round 2 (round 1 is finished, skipped by pair())
        self.assertEqual(rps.first().round, 2)

    def test_round_pairings_unique_constraint(self):
        RoundPairings.objects.create(division=self.division, round=1)
        with self.assertRaises(IntegrityError):
            RoundPairings.objects.create(division=self.division, round=1)

    def test_result_slip_pairing_fk_nullable(self):
        rs = ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrants[0],
            winner_score=450,
            loser=self.entrants[1],
            loser_score=380,
            winner_started=True,
        )
        self.assertIsNone(rs.pairing)

    # -- Step 4: regenerate preserves non-draft rounds --

    def test_regenerate_preserves_published_round(self):
        self._koth_config(2)
        self._regenerate()
        rp1 = RoundPairings.objects.get(division=self.division, round=1)
        rp1_pk = rp1.pk
        rp1.status = RoundPairings.PUBLISHED
        rp1.save()

        self._regenerate()

        # Published round preserved with same PK
        rp1 = RoundPairings.objects.get(pk=rp1_pk)
        self.assertEqual(rp1.status, RoundPairings.PUBLISHED)
        self.assertTrue(rp1.pairings.exists())

    def test_regenerate_preserves_finished_round_with_results(self):
        self._koth_config(2)
        self._regenerate()
        rp1 = RoundPairings.objects.get(division=self.division, round=1)
        rp1.status = RoundPairings.FINISHED
        rp1.save()
        pairing1 = rp1.pairings.first()
        rs = ResultSlip.objects.create(
            division=self.division,
            round=1,
            pairing=pairing1,
            winner=self.entrants[0],
            winner_score=450,
            loser=self.entrants[1],
            loser_score=380,
            winner_started=True,
        )
        rs_pk = rs.pk

        self._regenerate()

        # Round 1 and its data preserved
        self.assertTrue(RoundPairings.objects.filter(pk=rp1.pk).exists())
        self.assertTrue(rp1.pairings.exists())
        self.assertTrue(ResultSlip.objects.filter(pk=rs_pk).exists())

    def test_regenerate_with_fixed_pairings(self):
        self._koth_config(1)
        DBFixedPairing.objects.create(
            division=self.division,
            round_number=1,
            entrant1=self.entrants[0],
            entrant2=self.entrants[3],
        )
        self._regenerate()
        rp = RoundPairings.objects.get(division=self.division, round=1)
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        # Check that Alice-Dave pairing exists
        names = set()
        for p in rp.pairings.select_related("first__player", "second__player"):
            names.add(frozenset({p.first.player.name, p.second.player.name}))
        self.assertIn(frozenset({"Alice", "Dave"}), names)

    # -- Step 5: publish flow --

    def test_publish_transitions_draft_to_published(self):
        self._koth_config(1)
        self._regenerate()
        self.assertEqual(
            RoundPairings.objects.filter(
                division=self.division, status=RoundPairings.DRAFT
            ).count(),
            1,
        )
        RoundPairings.objects.filter(
            division=self.division, status=RoundPairings.DRAFT
        ).update(status=RoundPairings.PUBLISHED)
        self.assertEqual(
            RoundPairings.objects.filter(
                division=self.division, status=RoundPairings.PUBLISHED
            ).count(),
            1,
        )

    def test_publish_ignores_non_draft(self):
        self._koth_config(2)
        self._complete_round(1)
        self._regenerate()
        # We should have round 2 as draft. Manually create round 1 as in_progress.
        rp1 = RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.IN_PROGRESS
        )
        rp2 = RoundPairings.objects.get(division=self.division, round=2)
        self.assertEqual(rp2.status, RoundPairings.DRAFT)

        # Publish only drafts
        RoundPairings.objects.filter(
            division=self.division, status=RoundPairings.DRAFT
        ).update(status=RoundPairings.PUBLISHED)

        rp1.refresh_from_db()
        rp2.refresh_from_db()
        self.assertEqual(rp1.status, RoundPairings.IN_PROGRESS)
        self.assertEqual(rp2.status, RoundPairings.PUBLISHED)

    # -- Step 6: auto status transitions --

    def test_first_result_transitions_to_in_progress(self):
        self._koth_config(1)
        self._regenerate()
        rp = RoundPairings.objects.get(division=self.division, round=1)
        rp.status = RoundPairings.PUBLISHED
        rp.save()
        pairing = rp.pairings.first()
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairing,
            winner=self.entrants[0], winner_score=450,
            loser=self.entrants[1], loser_score=380,
            winner_started=True,
        )
        pairing.round_pairings.update_status()
        rp.refresh_from_db()
        self.assertEqual(rp.status, RoundPairings.IN_PROGRESS)

    def test_all_results_transitions_to_finished(self):
        self._koth_config(1)
        self._regenerate()
        rp = RoundPairings.objects.get(division=self.division, round=1)
        rp.status = RoundPairings.PUBLISHED
        rp.save()
        pairings = list(rp.pairings.all())
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairings[0],
            winner=pairings[0].first, winner_score=450,
            loser=pairings[0].second, loser_score=380,
            winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairings[1],
            winner=pairings[1].first, winner_score=400,
            loser=pairings[1].second, loser_score=350,
            winner_started=True,
        )
        pairings[0].round_pairings.update_status()
        rp.refresh_from_db()
        self.assertEqual(rp.status, RoundPairings.FINISHED)

    def test_result_deletion_resets_to_published(self):
        self._koth_config(1)
        self._regenerate()
        rp = RoundPairings.objects.get(division=self.division, round=1)
        rp.status = RoundPairings.PUBLISHED
        rp.save()
        pairing = rp.pairings.first()
        rs = ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairing,
            winner=self.entrants[0], winner_score=450,
            loser=self.entrants[1], loser_score=380,
            winner_started=True,
        )
        pairing.round_pairings.update_status()
        rp.refresh_from_db()
        self.assertEqual(rp.status, RoundPairings.IN_PROGRESS)
        # Delete the result — status should revert.
        rs.delete()
        pairing.round_pairings.update_status()
        rp.refresh_from_db()
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)

    def test_unpaired_round_not_marked_finished(self):
        # Regression: a published round with no pairings has 0 results and 0
        # pairings, so `with_results == total` is vacuously true (0 == 0). It
        # must NOT be treated as finished — a round with no games can't be
        # finished. Round-robin blocks create a RoundPairings row per round up
        # front, so this empty-but-published state is reachable; wrongly marking
        # it finished makes later rounds (which depend on it) look pairable.
        rp = RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.PUBLISHED,
        )
        self.assertEqual(rp.pairings.count(), 0)
        rp.update_status()
        rp.refresh_from_db()
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)


class RoundRobinUnplayedRoundsTests(PairingDBTestBase):
    """Regression: a round-robin configured with per-round start_round (as the
    settings editor stores it) must still pair every round up front, before any
    results exist — previously only the round whose start_round landed on the
    seedings got paired, and ranking the rest raised KeyError."""

    def _regenerate(self):
        from tournaments.generate_pairings import regenerate_pairings
        regenerate_pairings(self.division)

    def test_all_round_robin_rounds_pair_before_any_results(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
                {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 3, "start_round": 2, "pairing": "RoundRobin"},
            ],
        )
        self._regenerate()  # must not raise
        for rnd in (1, 2, 3):
            pairings = self.division.pairings.filter(round=rnd)
            self.assertEqual(pairings.count(), 2)
            names = set()
            for p in pairings:
                names.update({p.first_id, p.second_id})
            self.assertEqual(len(names), 4)  # every entrant paired exactly once

    def test_round_robin_block_after_other_rounds_pairs_up_front(self):
        # A round-robin block that does NOT start at round 1 (here it follows two
        # KotH rounds) seeds off the tournament seedings, so it must still pair
        # every round before any results — it must not read the (empty) standings
        # of the unplayed rounds before it and pair nobody.
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "start_round": 0, "pairing": "KotH"},
                {"round": 2, "start_round": 1, "pairing": "KotH"},
                {"round": 3, "start_round": 2, "pairing": "RoundRobin"},
                {"round": 4, "start_round": 3, "pairing": "RoundRobin"},
                {"round": 5, "start_round": 4, "pairing": "RoundRobin"},
            ],
        )
        self._regenerate()
        # Round 1 (KotH off seedings) pairs; round 2 depends on round 1's results
        # so it is not paired yet. The round-robin block pairs in full.
        self.assertEqual(self.division.pairings.filter(round=1).count(), 2)
        self.assertEqual(self.division.pairings.filter(round=2).count(), 0)
        for rnd in (3, 4, 5):
            pairings = self.division.pairings.filter(round=rnd)
            self.assertEqual(pairings.count(), 2, f"round {rnd}")
            names = set()
            for p in pairings:
                names.update({p.first_id, p.second_id})
            self.assertEqual(len(names), 4)  # every entrant paired exactly once
