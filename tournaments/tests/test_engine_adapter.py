"""Tests for the pairing engine adapter (tournaments/pairing/engine.py)."""

from unittest import TestCase

from tournaments.pairing.base import (
    EntrantData,
    PairingData,
    PairingError,
    PlayerData,
    Repeats,
    ResultSlipData,
)
from tournaments.pairing.engine import (
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


class EnginePathTests(TestCase):
    def test_engine_pairs_initial_swiss(self):
        # Dropped D is excluded → A/B/C + bye; A fixed to B, C takes the bye.
        out = pair_with_engine(_swiss_pd())
        self.assertEqual(len(out), 1)
        names = {(p.first.name, p.second.name) for _, ps in out for p in ps}
        self.assertIn(("A", "B"), names)

    def test_strict_swiss_impossible_pairing_reaches_python_as_error(self):
        names = ["A", "B", "C", "D"]
        completed_round_robin = [
            ResultSlipData(1, "A", "B", 450, 400, True),
            ResultSlipData(1, "C", "D", 450, 400, True),
            ResultSlipData(2, "A", "C", 450, 400, True),
            ResultSlipData(2, "B", "D", 450, 400, True),
            ResultSlipData(3, "A", "D", 450, 400, True),
            ResultSlipData(3, "B", "C", 450, 400, True),
        ]
        pairing_data = PairingData(
            result_slips=completed_round_robin,
            entrants=[
                EntrantData(PlayerData(name, 1600 - 10 * index))
                for index, name in enumerate(names)
            ],
            repeats=Repeats(),
            round_pairings=[
                RoundPairing(1, 1, RP.RoundRobin),
                RoundPairing(2, 1, RP.RoundRobin),
                RoundPairing(3, 1, RP.RoundRobin),
                RoundPairing(4, 3, RP.SwissNoRepeats),
            ],
        )

        with self.assertRaisesRegex(PairingError, "no repeat-free Swiss pairing"):
            pair_with_engine(pairing_data)


def _cop_pd(cop_config):
    """Six players, a finished Swiss round 1, then a COP round 2."""
    names = ["A", "B", "C", "D", "E", "F"]
    return PairingData(
        result_slips=[
            ResultSlipData(1, "A", "B", 450, 400, True),
            ResultSlipData(1, "C", "D", 450, 400, True),
            ResultSlipData(1, "E", "F", 450, 400, True),
        ],
        entrants=[EntrantData(PlayerData(n, 1600 - 10 * i)) for i, n in enumerate(names)],
        repeats=Repeats(),
        round_pairings=[RoundPairing(1, 0, RP.Swiss), RoundPairing(2, 1, RP.COP)],
        seed=7,
        cop_config=cop_config,
    )


_COP_CONFIG = {
    "place_prizes": 3,
    "gibson_spread": 250,
    "hopefulness": 0.05,
    "control_loss_threshold": 0.25,
    "control_loss_activation_round": 0,
    "simulations": 200,
    "always_wins_simulations": 100,
}


class CopConfigTests(TestCase):
    def test_scalar_config_expands_to_engine_shape(self):
        # The per-round-array fields become single-element arrays.
        d = pairing_data_to_input(_cop_pd(_COP_CONFIG))
        self.assertEqual(
            d["cop_config"],
            {
                "place_prizes": 3,
                "gibson_spreads": [250],
                "hopefulness": [0.05],
                "control_loss_thresholds": [0.25],
                "control_loss_activation_round": 0,
                "simulations": 200,
                "always_wins_simulations": 100,
                "disallow_repeat_byes": False,
            },
        )

    def test_empty_config_serializes_to_none(self):
        # No usable config → None, so a COP round fails loudly rather than pairing
        # on silent defaults.
        self.assertIsNone(pairing_data_to_input(_cop_pd(None))["cop_config"])
        self.assertIsNone(pairing_data_to_input(_cop_pd({}))["cop_config"])

    def test_engine_pairs_a_cop_round(self):
        out = pair_with_engine(_cop_pd(_COP_CONFIG))
        r2 = [ps for rnd, ps in out if rnd == 2]
        self.assertEqual(len(r2), 1)
        pairings = r2[0]
        self.assertEqual(len(pairings), 3)  # 6 players → 3 games
        paired = {p.first.name for p in pairings} | {p.second.name for p in pairings}
        self.assertEqual(paired, {"A", "B", "C", "D", "E", "F"})

    def test_cop_round_without_config_raises(self):
        with self.assertRaises(PairingError) as cm:
            pair_with_engine(_cop_pd(None))
        self.assertIn("cop_config", str(cm.exception))
