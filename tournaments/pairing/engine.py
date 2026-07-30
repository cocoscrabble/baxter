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


def _cop_config_to_input(c: dict | None) -> dict | None:
    """Expand DivisionSettings' scalar ``cop_config`` into the engine's
    ``CopConfig`` shape (``scrabble-pairing/src/model.rs``): the per-round-array
    fields become single-element arrays (the engine forward-fills them to the
    round count). Returns ``None`` when there's nothing usable, so a COP round
    without config fails loudly in the engine rather than pairing on defaults."""
    if not c or not c.get("place_prizes"):
        return None

    def arr(v):
        return v if isinstance(v, list) else [v]

    return {
        "place_prizes": int(c["place_prizes"]),
        "gibson_spreads": arr(c.get("gibson_spread", 250)),
        "hopefulness": arr(c.get("hopefulness", 0.05)),
        "control_loss_thresholds": arr(c.get("control_loss_threshold", 0.25)),
        "control_loss_activation_round": int(c.get("control_loss_activation_round", 0)),
        "simulations": int(c.get("simulations", 1000)),
        "always_wins_simulations": int(c.get("always_wins_simulations", 1000)),
        "disallow_repeat_byes": bool(c.get("disallow_repeat_byes", False)),
        # Count the rounds left from the round being paired rather than from
        # start_round. Only differs when a COP round pairs off an older snapshot
        # (pair_from > 1); see CopConfig in scrabble-pairing/src/model.rs.
        "horizon_from_paired_round": bool(c.get("horizon_from_paired_round", False)),
    }


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
        "published_pairings": {
            str(k): [[a, b] for (a, b) in v]
            for k, v in pd.published_pairings.items()
        },
        "seed": pd.seed,
        "cop_config": _cop_config_to_input(pd.cop_config),
        # Omitted keys fall back to the engine's defaults, so a partial config
        # (or none at all) pairs exactly as the hardcoded constants used to.
        "swiss_config": pd.swiss_config or {},
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
