"""Engine adapter: run pairing through the `scrabble_pairing_py` Rust extension.

The ORM-facing layer (``PairingData`` assembly, standings display, the
publish/regenerate lifecycle) stays in Python; only the pairing *computation* is
the Rust engine. ``pair_with_engine`` is the single entry point.
"""

import json

from tournaments.pairing.base import (
    DisplayPairing,
    PairingData,
    PairingError,
    Player,
)


def pairing_data_to_input(pd: PairingData) -> dict:
    """Serialize a ``PairingData`` into the Rust engine's JSON boundary shape
    (``scrabble-pairing/src/model.rs``)."""
    return {
        "players": [
            {"name": e.player.name, "rating": e.player.rating, "dropped": e.dropped}
            for e in pd.entrants
        ],
        "result_slips": [
            {
                "round": s.round,
                "winner_name": s.winner_name,
                "loser_name": s.loser_name,
                "winner_score": s.winner_score,
                "loser_score": s.loser_score,
                "winner_started": s.winner_started,
            }
            for s in pd.result_slips
        ],
        "round_pairings": [
            {"round": r.round, "start_round": r.start_round, "pairing": str(r.pairing)}
            for r in pd.round_pairings
        ],
        # JSON object keys are strings; the Rust side parses them back to i32.
        "fixed_pairings": {
            str(k): [[a, b] for (a, b) in v] for k, v in pd.fixed_pairings.items()
        },
        "seed": pd.seed,
    }


def pair_with_engine(pd: PairingData) -> list[tuple[int, list[DisplayPairing]]]:
    """Pair a whole tournament, returning ``[(round, [DisplayPairing, ...]), ...]``."""
    import scrabble_pairing_py

    raw = scrabble_pairing_py.pair_json(json.dumps(pairing_data_to_input(pd)))
    return _rounds_to_display(json.loads(raw))


def _rounds_to_display(rounds: list) -> list[tuple[int, list[DisplayPairing]]]:
    # The engine reports a bad round as ``error``; surface it as a PairingError
    # so the atomic regenerate_pairings rolls back cleanly (all-or-nothing).
    for r in rounds:
        if r.get("error"):
            raise PairingError(r["error"])
    return [
        (
            r["round"],
            [
                DisplayPairing(Player(p["first"]), Player(p["second"]), p["repeats"])
                for p in r["pairings"]
            ],
        )
        for r in rounds
    ]
