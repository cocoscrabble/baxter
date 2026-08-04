from unittest import TestCase

from tournaments.pairing.round_pairing import (
    RoundPairing,
    blocks_to_round_pairings,
    default_block_rounds,
    make_pairings,
    normalize_round_robin_start_rounds,
    round_pairings_to_blocks,
)


class MakePairingsTests(TestCase):
    def assert_pairings(self, spec, expected):
        result = make_pairings(spec)
        self.assertEqual([r.to_dict() for r in result], expected)

    def test_single_non_rr_round(self):
        self.assert_pairings(
            "KH",
            [
                {"round": 1, "start_round": 0, "pairing": "KotH"},
            ],
        )

    def test_multiple_non_rr_rounds(self):
        self.assert_pairings(
            "KH:3",
            [
                {"round": 1, "start_round": 0, "pairing": "KotH"},
                {"round": 2, "start_round": 1, "pairing": "KotH"},
                {"round": 3, "start_round": 2, "pairing": "KotH"},
            ],
        )

    def test_round_robin_shares_start_round(self):
        self.assert_pairings(
            "RR:3",
            [
                {"round": 1, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 3, "start_round": 1, "pairing": "RoundRobin"},
            ],
        )

    def test_double_round_robin(self):
        self.assert_pairings(
            "RR:3 RR:3",
            [
                {"round": 1, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 3, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 4, "start_round": 4, "pairing": "RoundRobin"},
                {"round": 5, "start_round": 4, "pairing": "RoundRobin"},
                {"round": 6, "start_round": 4, "pairing": "RoundRobin"},
            ],
        )

    def test_unknown_abbreviation_raises(self):
        with self.assertRaises(KeyError):
            make_pairings("CH:3")

    def test_mixed_spec(self):
        self.assert_pairings(
            "KH:2 SW:3",
            [
                {"round": 1, "start_round": 0, "pairing": "KotH"},
                {"round": 2, "start_round": 1, "pairing": "KotH"},
                {"round": 3, "start_round": 2, "pairing": "Swiss"},
                {"round": 4, "start_round": 3, "pairing": "Swiss"},
                {"round": 5, "start_round": 4, "pairing": "Swiss"},
            ],
        )

    def test_mixed_with_round_robin(self):
        self.assert_pairings(
            "RR:3 KH:2",
            [
                {"round": 1, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 3, "start_round": 1, "pairing": "RoundRobin"},
                {"round": 4, "start_round": 3, "pairing": "KotH"},
                {"round": 5, "start_round": 4, "pairing": "KotH"},
            ],
        )

    def test_implicit_count_of_one(self):
        self.assert_pairings(
            "SW",
            [
                {"round": 1, "start_round": 0, "pairing": "Swiss"},
            ],
        )

    def test_minimal_repeat_swiss_abbreviation(self):
        self.assert_pairings(
            "SM:2",
            [
                {"round": 1, "start_round": 0, "pairing": "SwissMinRepeats"},
                {"round": 2, "start_round": 1, "pairing": "SwissMinRepeats"},
            ],
        )

    def test_quads_share_start_round(self):
        # All rounds in a quad block share the same start_round (the round before the block).
        self.assert_pairings(
            "QC:3",
            [
                {"round": 1, "start_round": 0, "pairing": "Quads_Clustered"},
                {"round": 2, "start_round": 0, "pairing": "Quads_Clustered"},
                {"round": 3, "start_round": 0, "pairing": "Quads_Clustered"},
            ],
        )

    def test_quads_after_other_rounds(self):
        # start_round for a quad block is the last round before the block.
        self.assert_pairings(
            "KH:2 QC:3",
            [
                {"round": 1, "start_round": 0, "pairing": "KotH"},
                {"round": 2, "start_round": 1, "pairing": "KotH"},
                {"round": 3, "start_round": 2, "pairing": "Quads_Clustered"},
                {"round": 4, "start_round": 2, "pairing": "Quads_Clustered"},
                {"round": 5, "start_round": 2, "pairing": "Quads_Clustered"},
            ],
        )

    def test_sixes_share_start_round(self):
        self.assert_pairings(
            "SX:3",
            [
                {"round": 1, "start_round": 0, "pairing": "Sixes"},
                {"round": 2, "start_round": 0, "pairing": "Sixes"},
                {"round": 3, "start_round": 0, "pairing": "Sixes"},
            ],
        )


class NormalizeRoundRobinStartRoundsTests(TestCase):
    def _rps(self, rows):
        return [RoundPairing(**r) for r in rows]

    def test_per_round_start_rounds_collapse_to_block_first(self):
        # The settings editor stores start_round=round-1; round robin needs them
        # all pointing at the block's first round.
        rps = self._rps([
            {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
            {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
            {"round": 3, "start_round": 2, "pairing": "RoundRobin"},
        ])
        normalize_round_robin_start_rounds(rps)
        self.assertEqual([r.start_round for r in rps], [1, 1, 1])

    def test_non_round_robin_rounds_are_untouched(self):
        rps = self._rps([
            {"round": 1, "start_round": 0, "pairing": "Swiss"},
            {"round": 2, "start_round": 1, "pairing": "Swiss"},
        ])
        normalize_round_robin_start_rounds(rps)
        self.assertEqual([r.start_round for r in rps], [0, 1])

    def test_separate_blocks_keep_their_own_first_round(self):
        rps = self._rps([
            {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
            {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
            {"round": 3, "start_round": 2, "pairing": "Swiss"},
            {"round": 4, "start_round": 3, "pairing": "RoundRobin"},
        ])
        normalize_round_robin_start_rounds(rps)
        self.assertEqual([r.start_round for r in rps], [1, 1, 2, 4])


class BlocksToRoundPairingsTests(TestCase):
    def expand(self, blocks):
        return [r.to_dict() for r in blocks_to_round_pairings(blocks)]

    def test_sliding_pair_from_offset(self):
        # Swiss '2 before' -> start_round = round - 2 per round.
        self.assertEqual(
            self.expand([{"pairing": "Swiss", "rounds": 3, "pair_from": 2}]),
            [
                {"round": 1, "start_round": -1, "pairing": "Swiss"},
                {"round": 2, "start_round": 0, "pairing": "Swiss"},
                {"round": 3, "start_round": 1, "pairing": "Swiss"},
            ],
        )

    def test_quads_fixed_snapshot(self):
        # Quads (block starting at round 4) pair off one snapshot: blockStart - 1.
        rows = self.expand([
            {"pairing": "Swiss", "rounds": 3, "pair_from": 1},
            {"pairing": "Quads_Clustered", "rounds": 3, "pair_from": 1},
        ])
        quads = [r for r in rows if r["pairing"] == "Quads_Clustered"]
        self.assertEqual([r["round"] for r in quads], [4, 5, 6])
        self.assertEqual([r["start_round"] for r in quads], [3, 3, 3])

    def test_round_robin_pairs_off_block_start(self):
        rows = self.expand([{"pairing": "RoundRobin", "rounds": 3, "pair_from": 1}])
        self.assertEqual([r["start_round"] for r in rows], [1, 1, 1])


class DefaultBlockRoundsTests(TestCase):
    def test_round_robin_and_charlottesville_scale_with_field(self):
        d = default_block_rounds(8)
        self.assertEqual(d["RoundRobin"], 7)
        self.assertEqual(d["DoubleRoundRobin"], 14)
        self.assertEqual(d["Charlottesville"], 4)
        self.assertEqual(d["Quads_Clustered"], 3)
        self.assertEqual(d["Sixes"], 3)

    def test_odd_field_round_robin(self):
        self.assertEqual(default_block_rounds(7)["RoundRobin"], 7)


class RoundPairingsToBlocksTests(TestCase):
    def test_groups_consecutive_runs_and_infers_pair_from(self):
        blocks = round_pairings_to_blocks([
            {"round": 1, "start_round": -1, "pairing": "Swiss"},
            {"round": 2, "start_round": 0, "pairing": "Swiss"},
            {"round": 3, "start_round": 3, "pairing": "RoundRobin"},
        ])
        self.assertEqual(blocks, [
            {"pairing": "Swiss", "rounds": 2, "pair_from": 2},  # 1 - (-1)
            {"pairing": "RoundRobin", "rounds": 1, "pair_from": 1},  # RR ignores it
        ])

    def test_same_strategy_different_pair_from_splits(self):
        # Swiss 1-before then Swiss 2-before must be two blocks, not one.
        blocks = round_pairings_to_blocks([
            {"round": 1, "start_round": 0, "pairing": "Swiss"},
            {"round": 2, "start_round": 1, "pairing": "Swiss"},
            {"round": 3, "start_round": 1, "pairing": "Swiss"},
            {"round": 4, "start_round": 2, "pairing": "Swiss"},
        ])
        self.assertEqual(blocks, [
            {"pairing": "Swiss", "rounds": 2, "pair_from": 1},
            {"pairing": "Swiss", "rounds": 2, "pair_from": 2},
        ])

    def test_consecutive_round_robin_rounds_collapse_to_one_block(self):
        # Legacy schedules stored per-round start_rounds for RR; they should
        # still seed a single round-robin block.
        blocks = round_pairings_to_blocks([
            {"round": 1, "start_round": 0, "pairing": "RoundRobin"},
            {"round": 2, "start_round": 1, "pairing": "RoundRobin"},
            {"round": 3, "start_round": 2, "pairing": "RoundRobin"},
        ])
        self.assertEqual(blocks, [{"pairing": "RoundRobin", "rounds": 3, "pair_from": 1}])

    def test_quads_stay_one_block_despite_varying_offset(self):
        # Quads share a fixed start_round; round-start_round varies (1,2,3) but
        # they must remain a single block.
        blocks = round_pairings_to_blocks([
            {"round": 1, "start_round": 0, "pairing": "Quads_Clustered"},
            {"round": 2, "start_round": 0, "pairing": "Quads_Clustered"},
            {"round": 3, "start_round": 0, "pairing": "Quads_Clustered"},
        ])
        self.assertEqual(blocks, [{"pairing": "Quads_Clustered", "rounds": 3, "pair_from": 1}])
