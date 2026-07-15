"""Smoke tests for the scrabble_pairing_py PyO3 extension.

Just checks the module imports and the JSON boundary produces the expected shape.
Engine *behaviour* is covered by the Rust crate's own tests and the parity
corpus; here we only confirm the wheel is built and callable from Python.
"""

import json
from unittest import TestCase

import scrabble_pairing_py


class RustEngineSmokeTest(TestCase):
    def _pair(self, payload):
        return json.loads(scrabble_pairing_py.pair_json(json.dumps(payload)))

    def test_initial_swiss_pairs_top_vs_bottom_half(self):
        out = self._pair(
            {
                "players": [
                    {"name": "A", "rating": 1600},
                    {"name": "B", "rating": 1500},
                    {"name": "C", "rating": 1400},
                    {"name": "D", "rating": 1300},
                ],
                "round_pairings": [
                    {"round": 1, "start_round": 0, "pairing": "Swiss"}
                ],
            }
        )
        self.assertEqual(len(out), 1)
        pairs = [(p["first"], p["second"]) for p in out[0]["pairings"]]
        # Swiss initial: top half vs bottom half.
        self.assertEqual(pairs, [("A", "C"), ("B", "D")])
        self.assertIsNone(out[0]["error"])

    def test_invalid_json_raises_valueerror(self):
        with self.assertRaises(ValueError):
            scrabble_pairing_py.pair_json("not json")
