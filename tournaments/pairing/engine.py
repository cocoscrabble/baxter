"""Engine adapter: run pairing through the Python engine, the Rust engine
(`scrabble_pairing_py`), or both (shadow mode).

The ORM-facing layer (``PairingData`` assembly, standings display, the
publish/regenerate lifecycle) stays in Python; only the pairing *computation* is
swapped. Selected by ``settings.PAIRING_ENGINE`` (``"python" | "rust" |
"shadow"``, default ``"python"``).
"""

import json
import logging

from django.conf import settings

from tournaments.pairing.base import (
    DisplayPairing,
    PairingData,
    PairingError,
    Player,
)
from tournaments.pairing.pair import pair
from tournaments.pairing.round_pairing import RP

logger = logging.getLogger(__name__)

# Random strategies use the engines' RNGs differently (Python: unseeded global
# RNG; Rust: the explicit per-division seed), so shadow mode can't compare their
# rounds exactly — it skips them and checks only the deterministic ones.
RANDOM_STRATEGIES = {RP.Random, RP.RandomNoRepeats, RP.SwissPlusRandom}


def pairing_data_to_input(pd: PairingData) -> dict:
    """Serialize a ``PairingData`` into the Rust engine's JSON boundary shape
    (``scrabble-pairing/src/model.rs``). Also the single serializer the parity
    corpus exporter uses, so the two never drift.
    """
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
    """Pair a whole tournament through the configured engine, returning the same
    ``[(round, [DisplayPairing, ...]), ...]`` shape as the Python ``pair()``."""
    engine = getattr(settings, "PAIRING_ENGINE", "python")
    if engine == "rust":
        return _pair_rust(pd)
    if engine == "shadow":
        return _pair_shadow(pd)
    return pair(pd)


def _pair_rust(pd: PairingData) -> list[tuple[int, list[DisplayPairing]]]:
    # Imported lazily so a missing extension only breaks the rust/shadow paths,
    # not `import tournaments.pairing.engine` on the default python path.
    import scrabble_pairing_py

    raw = scrabble_pairing_py.pair_json(json.dumps(pairing_data_to_input(pd)))
    return _rounds_to_display(json.loads(raw))


def _rounds_to_display(rounds: list) -> list[tuple[int, list[DisplayPairing]]]:
    # The Rust engine reports a bad round as ``error`` and keeps going; the
    # Python engine raises and aborts the whole regeneration. Preserve the
    # all-or-nothing semantics: any error round fails the run. (regenerate_pairings
    # is atomic, so the raise rolls back cleanly.)
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


def _pair_shadow(pd: PairingData) -> list[tuple[int, list[DisplayPairing]]]:
    """Return the Python result, but also run the Rust engine and log any
    divergence on the deterministic rounds. A Rust-side failure never breaks the
    request — it's logged and the Python result is returned."""
    python_result = pair(pd)
    try:
        rust_result = _pair_rust(pd)
    except Exception:
        logger.exception(
            "shadow: rust engine raised; input=%s",
            json.dumps(pairing_data_to_input(pd)),
        )
        return python_result
    _log_shadow_divergence(pd, python_result, rust_result)
    return python_result


def _log_shadow_divergence(pd, python_result, rust_result) -> None:
    strategy_by_round = {r.round: r.pairing for r in pd.round_pairings}

    def normalize(result):
        return {
            rnd: [(p.first.name, p.second.name, p.repeats) for p in pairings]
            for rnd, pairings in result
        }

    py = normalize(python_result)
    ru = normalize(rust_result)
    for rnd in sorted(set(py) | set(ru)):
        if strategy_by_round.get(rnd) in RANDOM_STRATEGIES:
            continue
        if py.get(rnd) != ru.get(rnd):
            logger.error(
                "shadow: engine divergence in round %s\n python=%s\n rust=%s\n input=%s",
                rnd,
                py.get(rnd),
                ru.get(rnd),
                json.dumps(pairing_data_to_input(pd)),
            )
