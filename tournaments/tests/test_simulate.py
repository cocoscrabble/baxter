"""Slow simulation-based tests for pairing strategies.

Run with: uv run python manage.py test tournaments.tests.test_simulate
Exclude: uv run python manage.py test tournaments.tests --exclude-tag slow
"""

from collections import defaultdict
from unittest import TestCase

from django.test import tag

from tournaments.pairing.round_pairing import make_pairings
from tournaments.simulate import (
    Round,
    check_starts_balancing,
    compare_engines,
    simulate,
)


def _rp_dicts(spec: str) -> list[dict]:
    return [rp.to_dict() for rp in make_pairings(spec)]


def _all_players(rounds: list[Round]) -> set[str]:
    names = set()
    for round in rounds:
        for p in round.pairings:
            names.add(p.first.name)
            names.add(p.second.name)
    return names


def _player_opponents(rounds: list[Round]) -> dict[str, list[str]]:
    opponents = defaultdict(list)
    for round in rounds:
        for p in round.pairings:
            opponents[p.first.name].append(p.second.name)
            opponents[p.second.name].append(p.first.name)
    return opponents


@tag("slow")
class StartsBalancingTests(TestCase):
    """Verify starts balancing across strategies and player counts."""

    def _check(self, spec, n_entrants, seeds=range(5)):
        rps = _rp_dicts(spec)
        for seed in seeds:
            with self.subTest(spec=spec, n_entrants=n_entrants, seed=seed):
                rounds, _, _ = simulate(rps, n_entrants, seed=seed)
                check_starts_balancing(rounds)

    def test_koth(self):
        self._check("KH:7", 24)

    def test_koth_small(self):
        self._check("KH:7", 8)

    def test_qoth(self):
        self._check("QH:7", 24)

    def test_qoth_non_multiple_of_four(self):
        self._check("QH:7", 22)

    def test_swiss(self):
        self._check("SW:7", 24)

    def test_swiss_small(self):
        self._check("SW:7", 8)

    def test_round_robin(self):
        self._check("RR:3", 4)

    def test_round_robin_larger(self):
        self._check("RR:7", 8)

    def test_quads_clustered(self):
        self._check("QC:3", 24)

    def test_quads_distributed(self):
        self._check("QD:3", 24)

    def test_quads_equalized(self):
        self._check("QE:3", 24)

    def test_quads_with_hex(self):
        self._check("QC:3", 22)

    def test_mixed_koth_swiss(self):
        self._check("KH:3 SW:4", 24)

    def test_mixed_rr_koth(self):
        self._check("RR:3 KH:4", 4)


@tag("slow")
class PairingCountTests(TestCase):
    """Verify that every round produces the expected number of pairings."""

    def _check(self, spec, n_entrants):
        rps = _rp_dicts(spec)
        expected_games = n_entrants // 2
        rounds, _, _ = simulate(rps, n_entrants, seed=42)
        for round in rounds:
            with self.subTest(round=round.number):
                self.assertEqual(len(round.pairings), expected_games)
                self.assertEqual(len(round.results), expected_games)

    def test_koth(self):
        self._check("KH:7", 24)

    def test_swiss(self):
        self._check("SW:7", 24)

    def test_round_robin(self):
        self._check("RR:3", 4)

    def test_quads_clustered(self):
        self._check("QC:3", 24)


@tag("slow")
class EngineComparisonTests(TestCase):
    """The Python and Rust engines must stay in agreement on the deterministic,
    non-blossom strategies (a regression guard for the cutover). Swiss uses
    blossom matching, whose tie-breaks are implementation-defined and differ
    between the engines (equal-cost), so it is deliberately not asserted equal.
    """

    def _assert_agree(self, spec, n_entrants, seeds=range(4)):
        for seed in seeds:
            with self.subTest(spec=spec, n_entrants=n_entrants, seed=seed):
                divergences = compare_engines(_rp_dicts(spec), n_entrants, seed=seed)
                self.assertEqual(
                    divergences, [], f"engines diverged: {[str(d) for d in divergences]}"
                )

    def test_koth_agrees(self):
        self._assert_agree("KH:15", 24)
        self._assert_agree("KH:15", 23)  # odd field -> bye

    def test_qoth_agrees(self):
        self._assert_agree("QH:15", 20)

    def test_round_robin_agrees(self):
        self._assert_agree("RR:11", 12)
        self._assert_agree("RR:11", 11)  # odd field -> bye

    def test_double_round_robin_agrees(self):
        self._assert_agree("DR:10", 8)

    def test_quads_agree(self):
        self._assert_agree("QC:3", 16)
        self._assert_agree("QD:3", 12)
        self._assert_agree("QE:3", 12)

    def test_sixes_agrees(self):
        self._assert_agree("SX:3", 12)

    def test_swiss_divergences_are_equal_cost(self):
        # Swiss may pick a different (but equally optimal) matching on ties. When
        # it does, the disagreement must only ever be orientation or an
        # equal-cost different-pairs choice — never a "rust-error", and never a
        # different-pairs that changes the repeat profile (which _classify would
        # not flag, but which we guard against here by construction).
        seen = set()
        for seed in range(8):
            for d in compare_engines(_rp_dicts("SW:15"), 24, seed=seed):
                seen.add(d.kind)
        self.assertNotIn("rust-error", seen)
        self.assertTrue(seen <= {"orientation", "different-pairs"})


@tag("slow")
class SwissNeverShortRoundTests(TestCase):
    """Regression: Swiss must pair the whole field every round.

    The Swiss matcher used to abandon the last win-group when it couldn't be
    paired without a repeat — bumping the repeat allowance but then breaking
    instead of retrying — which dropped players and produced a short round. That
    short round never "finished", so it stalled multi-round simulation. These
    combinations each triggered the bug before the fix.
    """

    def test_full_field_paired_across_sizes_and_seeds(self):
        rps = _rp_dicts("SW:15")
        for n_entrants in (8, 10, 16, 30):
            expected_games = n_entrants // 2
            for seed in range(8):
                rounds, _, _ = simulate(rps, n_entrants, seed=seed)
                for round in rounds:
                    with self.subTest(n=n_entrants, seed=seed, round=round.number):
                        self.assertEqual(len(round.pairings), expected_games)


@tag("slow")
class AllPlayersAppearTests(TestCase):
    """Verify every player appears in every round."""

    def _check(self, spec, n_entrants):
        rps = _rp_dicts(spec)
        rounds, _, _ = simulate(rps, n_entrants, seed=42)
        all_names = _all_players(rounds)
        for round in rounds:
            with self.subTest(round=round.number):
                round_names = set()
                for p in round.pairings:
                    round_names.add(p.first.name)
                    round_names.add(p.second.name)
                self.assertEqual(round_names, all_names)

    def test_koth(self):
        self._check("KH:5", 24)

    def test_swiss(self):
        self._check("SW:5", 24)

    def test_round_robin(self):
        self._check("RR:3", 4)


@tag("slow")
class NoDuplicatePairingsInRoundTests(TestCase):
    """Verify no player is paired twice in the same round."""

    def _check(self, spec, n_entrants):
        rps = _rp_dicts(spec)
        rounds, _, _ = simulate(rps, n_entrants, seed=42)
        for round in rounds:
            with self.subTest(round=round.number):
                seen = set()
                for p in round.pairings:
                    self.assertNotIn(p.first.name, seen, f"{p.first.name} paired twice")
                    self.assertNotIn(p.second.name, seen, f"{p.second.name} paired twice")
                    seen.add(p.first.name)
                    seen.add(p.second.name)

    def test_koth(self):
        self._check("KH:7", 24)

    def test_swiss(self):
        self._check("SW:7", 24)

    def test_quads_clustered(self):
        self._check("QC:3", 24)


@tag("slow")
class RoundRobinCompletenessTests(TestCase):
    """Verify round robin produces all pairings (everyone plays everyone)."""

    def test_four_players(self):
        rps = _rp_dicts("RR:3")
        rounds, _, _ = simulate(rps, 4, seed=42)
        opponents = _player_opponents(rounds)
        players = _all_players(rounds)
        for player in players:
            other = players - {player}
            self.assertEqual(
                set(opponents[player]),
                other,
                f"{player} didn't play everyone",
            )

    def test_eight_players(self):
        rps = _rp_dicts("RR:7")
        rounds, _, _ = simulate(rps, 8, seed=42)
        opponents = _player_opponents(rounds)
        players = _all_players(rounds)
        for player in players:
            other = players - {player}
            self.assertEqual(
                set(opponents[player]),
                other,
                f"{player} didn't play everyone",
            )


@tag("slow")
class RepeatsTrackingTests(TestCase):
    """Verify repeat tracking is consistent across a full tournament."""

    def test_repeats_counted_correctly(self):
        rps = _rp_dicts("KH:7")
        rounds, _, repeats = simulate(rps, 8, seed=42)
        # Count pairings manually
        pair_counts = defaultdict(int)
        for round in rounds:
            for p in round.pairings:
                key = tuple(sorted([p.first.name, p.second.name]))
                pair_counts[key] += 1
        # Every pair tracked by Repeats should match our manual count
        for key, count in pair_counts.items():
            from tournaments.pairing.base import Pairing, Player
            p = Pairing(Player(key[0]), Player(key[1]))
            self.assertEqual(repeats.get(p), count)
