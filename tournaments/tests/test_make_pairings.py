from unittest import TestCase

from tournaments.pairing.round_pairing import make_pairings


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
