"""Slow simulation-based tests for pairing strategies.

Run with: uv run python manage.py test tournaments.tests.test_simulate
Exclude: uv run python manage.py test tournaments.tests --exclude-tag slow
"""

from collections import defaultdict
from unittest import TestCase

from django.test import tag

from tournaments.pairing.methods import swiss_contenders_schedule
from tournaments.pairing.round_pairing import blocks_to_round_pairings, make_pairings
from tournaments.simulate import (
    Round,
    check_starts_balancing,
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


def _swiss_contenders_pre_cop_schedule(n_entrants: int) -> list[dict]:
    """Expand the no-repeat and minimal-repeat Swiss phases of a 24-round event."""
    schedule = swiss_contenders_schedule(entrants=n_entrants, total_rounds=24)
    return [
        pairing.to_dict() for pairing in blocks_to_round_pairings(schedule.blocks[:-1])
    ]


def _assert_no_real_repeats(test_case: TestCase, rounds: list[Round]) -> None:
    seen = set()
    for round in rounds:
        for pairing in round.pairings:
            names = frozenset((pairing.first.name, pairing.second.name))
            if "Bye" in names:
                continue
            test_case.assertNotIn(
                names, seen, f"repeated pairing in round {round.number}"
            )
            seen.add(names)


def _minimum_repeated_games(
    players: tuple[str, ...], played: set[frozenset[str]]
) -> int:
    """Brute-force the fewest already-played edges in a perfect matching."""

    if not players:
        return 0
    first = players[0]
    best = len(players)
    for index in range(1, len(players)):
        opponent = players[index]
        remaining = players[1:index] + players[index + 1 :]
        repeat = int(frozenset((first, opponent)) in played)
        best = min(best, repeat + _minimum_repeated_games(remaining, played))
    return best


@tag("slow")
class SwissContendersSimulationTests(TestCase):
    """Exercise both Swiss thirds through the real engine."""

    def test_minimal_repeat_swiss_uses_repeats_only_when_globally_unavoidable(self):
        n_entrants = 8
        schedule = swiss_contenders_schedule(
            entrants=n_entrants,
            total_rounds=14,
        )
        pre_cop = [
            pairing.to_dict()
            for pairing in blocks_to_round_pairings(schedule.blocks[:-1])
        ]

        required_repeats = set()
        for seed in range(5):
            with self.subTest(seed=seed):
                rounds = simulate(pre_cop, n_entrants, seed=seed)
                no_repeat_rounds = rounds[:5]
                first_minimal_repeat_round = rounds[5]
                _assert_no_real_repeats(self, no_repeat_rounds)

                played = {
                    frozenset((pairing.first.name, pairing.second.name))
                    for round in no_repeat_rounds
                    for pairing in round.pairings
                }
                players = tuple(f"Player {number}" for number in range(1, 9))
                minimum = _minimum_repeated_games(players, played)
                actual = sum(
                    frozenset((pairing.first.name, pairing.second.name)) in played
                    for pairing in first_minimal_repeat_round.pairings
                )

                self.assertEqual(actual, minimum)
                required_repeats.add(minimum)

        # Exercise both sides of the rule across the deterministic cases.
        self.assertIn(0, required_repeats)
        self.assertTrue(any(repeats > 0 for repeats in required_repeats))

    def test_minimal_repeat_swiss_starts_after_no_repeat_capacity_is_exhausted(self):
        n_entrants = 6
        schedule = swiss_contenders_schedule(
            entrants=n_entrants,
            total_rounds=14,
        )
        pre_cop = [
            pairing.to_dict()
            for pairing in blocks_to_round_pairings(schedule.blocks[:-1])
        ]

        rounds = simulate(pre_cop, n_entrants, seed=0)
        no_repeat_rounds = rounds[:5]
        first_minimal_repeat_round = rounds[5]

        # Five rounds exhaust all C(6, 2) = 15 possible opponents exactly once.
        _assert_no_real_repeats(self, no_repeat_rounds)
        exhausted_pairs = {
            frozenset((pairing.first.name, pairing.second.name))
            for round in no_repeat_rounds
            for pairing in round.pairings
        }
        self.assertEqual(len(exhausted_pairs), 15)

        # A sixth no-repeat round is impossible. Minimal-repeat Swiss must still
        # pair a complete round, using one unavoidable repeat per player.
        self.assertEqual(len(first_minimal_repeat_round.pairings), 3)
        for pairing in first_minimal_repeat_round.pairings:
            names = frozenset((pairing.first.name, pairing.second.name))
            self.assertIn(names, exhausted_pairs)

    def test_even_nacc_fields_pair_every_player_in_both_swiss_phases(self):
        for n_entrants in (18, 22):
            schedule = _swiss_contenders_pre_cop_schedule(n_entrants)
            expected_names = {f"Player {number}" for number in range(1, n_entrants + 1)}
            for seed in range(5):
                with self.subTest(n_entrants=n_entrants, seed=seed):
                    rounds = simulate(schedule, n_entrants, seed=seed)

                    self.assertEqual(len(rounds), 16)
                    for round in rounds:
                        self.assertEqual(len(round.pairings), n_entrants // 2)
                        self.assertEqual(len(round.results), n_entrants // 2)
                        names = {
                            player
                            for pairing in round.pairings
                            for player in (pairing.first.name, pairing.second.name)
                        }
                        self.assertEqual(names, expected_names)
                    _assert_no_real_repeats(self, rounds[:8])
                    check_starts_balancing(rounds)

    def test_odd_field_rotates_byes_during_the_no_repeat_swiss_third(self):
        n_entrants = 23
        schedule = _swiss_contenders_pre_cop_schedule(n_entrants)
        expected_names = {f"Player {number}" for number in range(1, n_entrants + 1)}
        for seed in range(5):
            with self.subTest(seed=seed):
                rounds = simulate(schedule, n_entrants, seed=seed)
                bye_recipients = []

                self.assertEqual(len(rounds), 16)
                for round in rounds:
                    self.assertEqual(len(round.pairings), 12)
                    real_names = set()
                    round_byes = []
                    for pairing in round.pairings:
                        names = {pairing.first.name, pairing.second.name}
                        real_names.update(names - {"Bye"})
                        if "Bye" in names:
                            round_byes.extend(names - {"Bye"})
                    self.assertEqual(real_names, expected_names)
                    self.assertEqual(len(round_byes), 1)
                    if round.number <= 8:
                        bye_recipients.extend(round_byes)

                self.assertEqual(len(bye_recipients), 8)
                self.assertEqual(len(set(bye_recipients)), 8)
                _assert_no_real_repeats(self, rounds[:8])


@tag("slow")
class StartsBalancingTests(TestCase):
    """Verify starts balancing across strategies and player counts."""

    def _check(self, spec, n_entrants, seeds=range(5)):
        rps = _rp_dicts(spec)
        for seed in seeds:
            with self.subTest(spec=spec, n_entrants=n_entrants, seed=seed):
                rounds = simulate(rps, n_entrants, seed=seed)
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
        rounds = simulate(rps, n_entrants, seed=42)
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
                rounds = simulate(rps, n_entrants, seed=seed)
                for round in rounds:
                    with self.subTest(n=n_entrants, seed=seed, round=round.number):
                        self.assertEqual(len(round.pairings), expected_games)


@tag("slow")
class AllPlayersAppearTests(TestCase):
    """Verify every player appears in every round."""

    def _check(self, spec, n_entrants):
        rps = _rp_dicts(spec)
        rounds = simulate(rps, n_entrants, seed=42)
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
        rounds = simulate(rps, n_entrants, seed=42)
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
        rounds = simulate(rps, 4, seed=42)
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
        rounds = simulate(rps, 8, seed=42)
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
        rounds = simulate(rps, 8, seed=42)
        # A pairing's repeat count is how many times that pair has met, counting
        # the current round (the engine returns the post-increment count).
        seen = defaultdict(int)
        for round in rounds:
            for p in round.pairings:
                key = tuple(sorted([p.first.name, p.second.name]))
                seen[key] += 1
                self.assertEqual(p.repeats, seen[key])
