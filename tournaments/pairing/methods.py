"""Schedule-level pairing methods.

Round pairing strategies answer "how should this round be paired?".  A pairing
method answers the larger question: "which strategies should this tournament
use, and when?".  Methods compile to the ordinary editable block format so the
pairing engine and the schedule editor keep a single source of truth.
"""

from dataclasses import dataclass
from enum import StrEnum

from .round_pairing import RP


MIN_SWISS_CONTENDERS_ROUNDS = 14


class PairingMethod(StrEnum):
    SWISS_CONTENDERS = "SwissContenders"

    @property
    def label(self) -> str:
        return {self.SWISS_CONTENDERS: "Swiss Contenders"}[self]


@dataclass(frozen=True)
class PairingMethodSchedule:
    """The editable blocks produced by a schedule-level pairing method."""

    method: PairingMethod
    blocks: list[dict]


def _block(pairing: RP, rounds: int) -> dict:
    return {"pairing": str(pairing), "rounds": rounds, "pair_from": 1}


def _no_repeat_capacity(entrants: int) -> int:
    """Maximum rounds possible before an opponent (or bye) must repeat."""

    # An even field has entrants - 1 distinct opponents. An odd field can also
    # schedule one distinct bye per player, so its complete rotation is one
    # round longer.
    return entrants - 1 if entrants % 2 == 0 else entrants


def swiss_contenders_schedule(
    *,
    entrants: int,
    total_rounds: int,
) -> PairingMethodSchedule:
    """Build CoCo's Swiss Contenders schedule.

    Divide the event into thirds: strict no-repeat Swiss, Swiss minimizing
    repeats, then COP. If the round count is not divisible by three, assign the
    first extra round to the middle Swiss phase and the second to strict Swiss.
    """

    if total_rounds < MIN_SWISS_CONTENDERS_ROUNDS:
        raise ValueError(
            f"Swiss Contenders requires at least {MIN_SWISS_CONTENDERS_ROUNDS} rounds."
        )
    if entrants < 2:
        raise ValueError("Swiss Contenders requires at least two entrants.")

    rounds_per_phase, extra_rounds = divmod(total_rounds, 3)
    no_repeat_rounds = rounds_per_phase + (1 if extra_rounds == 2 else 0)
    minimal_repeat_rounds = rounds_per_phase + (1 if extra_rounds >= 1 else 0)
    cop_rounds = rounds_per_phase

    capacity = _no_repeat_capacity(entrants)
    if no_repeat_rounds > capacity:
        raise ValueError(
            f"Swiss Contenders needs {no_repeat_rounds} no-repeat rounds, but "
            f"{entrants} entrants can support at most {capacity}."
        )

    blocks = [
        _block(RP.SwissNoRepeats, no_repeat_rounds),
        _block(RP.Swiss, minimal_repeat_rounds),
        _block(RP.COP, cop_rounds),
    ]

    return PairingMethodSchedule(
        method=PairingMethod.SWISS_CONTENDERS,
        blocks=blocks,
    )


def pairing_method_schedule(
    method: PairingMethod,
    *,
    entrants: int,
    total_rounds: int,
) -> PairingMethodSchedule:
    """Dispatch a first-class pairing method to its schedule builder."""

    if method == PairingMethod.SWISS_CONTENDERS:
        return swiss_contenders_schedule(
            entrants=entrants,
            total_rounds=total_rounds,
        )
    raise ValueError(f"Unsupported pairing method: {method}")
