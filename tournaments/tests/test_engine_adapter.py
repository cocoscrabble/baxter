"""Tests for the pairing engine adapter (tournaments/pairing/engine.py)."""

from unittest import TestCase, mock

from django.test import override_settings

from tournaments.pairing.base import (
    EntrantData,
    PairingData,
    PairingError,
    PlayerData,
    Repeats,
    ResultSlipData,
)
from tournaments.pairing.engine import (
    _pair_shadow,
    _rounds_to_display,
    pair_with_engine,
    pairing_data_to_input,
)
from tournaments.pairing.round_pairing import RP, RoundPairing


def _swiss_pd():
    return PairingData(
        result_slips=[],
        entrants=[
            EntrantData(PlayerData("A", 1600)),
            EntrantData(PlayerData("B", 1500)),
            EntrantData(PlayerData("C", 1400)),
            EntrantData(PlayerData("D", 1300), dropped=True),
        ],
        repeats=Repeats(),
        round_pairings=[RoundPairing(1, 0, RP.Swiss)],
        fixed_pairings={1: [("A", "B")]},
        seed=42,
    )


class SerializerTests(TestCase):
    def test_boundary_shape(self):
        d = pairing_data_to_input(_swiss_pd())
        self.assertEqual(
            d["players"],
            [
                {"name": "A", "rating": 1600, "dropped": False},
                {"name": "B", "rating": 1500, "dropped": False},
                {"name": "C", "rating": 1400, "dropped": False},
                {"name": "D", "rating": 1300, "dropped": True},
            ],
        )
        self.assertEqual(
            d["round_pairings"],
            [{"round": 1, "start_round": 0, "pairing": "Swiss"}],
        )
        # Fixed-pairing keys are stringified for the JSON boundary.
        self.assertEqual(d["fixed_pairings"], {"1": [["A", "B"]]})
        self.assertEqual(d["seed"], 42)

    def test_result_slips_serialized_field_for_field(self):
        pd = _swiss_pd()
        pd.result_slips = [ResultSlipData(1, "A", "B", 450, 380, True)]
        d = pairing_data_to_input(pd)
        self.assertEqual(
            d["result_slips"],
            [
                {
                    "round": 1,
                    "winner_name": "A",
                    "loser_name": "B",
                    "winner_score": 450,
                    "loser_score": 380,
                    "winner_started": True,
                }
            ],
        )


class RoundsToDisplayTests(TestCase):
    def test_maps_names_and_repeats(self):
        rounds = [
            {
                "round": 1,
                "pairings": [{"first": "A", "second": "C", "repeats": 2}],
                "error": None,
            }
        ]
        out = _rounds_to_display(rounds)
        self.assertEqual(len(out), 1)
        rnd, pairings = out[0]
        self.assertEqual(rnd, 1)
        self.assertEqual(pairings[0].first.name, "A")
        self.assertEqual(pairings[0].second.name, "C")
        self.assertEqual(pairings[0].repeats, 2)

    def test_error_round_raises_pairing_error(self):
        rounds = [{"round": 1, "pairings": [], "error": "unsatisfiable fixed pairings"}]
        with self.assertRaises(PairingError) as cm:
            _rounds_to_display(rounds)
        self.assertIn("unsatisfiable", str(cm.exception))


class RustPathTests(TestCase):
    @override_settings(PAIRING_ENGINE="rust")
    def test_rust_engine_pairs_initial_swiss(self):
        # Dropped D is excluded → A/B/C + bye; A-C paired, B byes (deterministic).
        out = pair_with_engine(_swiss_pd())
        self.assertEqual(len(out), 1)
        names = {
            (p.first.name, p.second.name) for _, ps in out for p in ps
        }
        # A is fixed to B this round; C takes the bye (odd active field).
        self.assertIn(("A", "B"), names)


class ShadowModeTests(TestCase):
    def test_divergence_is_logged_and_python_result_returned(self):
        pd = _swiss_pd()
        # Force a divergence by mutating the rust result's deterministic round.
        bad = [
            {
                "round": 1,
                "pairings": [{"first": "Z", "second": "Y", "repeats": 0}],
                "error": None,
            }
        ]
        with mock.patch(
            "tournaments.pairing.engine._pair_rust",
            return_value=_rounds_to_display(bad),
        ):
            with self.assertLogs("tournaments.pairing.engine", level="ERROR") as logs:
                result = _pair_shadow(pd)
        self.assertTrue(any("divergence" in m for m in logs.output))
        # The Python result is returned unchanged (A-B fixed pairing present).
        pairs = {(p.first.name, p.second.name) for _, ps in result for p in ps}
        self.assertIn(("A", "B"), pairs)

    def test_rust_exception_does_not_break_shadow(self):
        pd = _swiss_pd()
        with mock.patch(
            "tournaments.pairing.engine._pair_rust",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("tournaments.pairing.engine", level="ERROR"):
                result = _pair_shadow(pd)
        # Still returns the Python result despite the rust-side blow-up.
        self.assertTrue(result)
