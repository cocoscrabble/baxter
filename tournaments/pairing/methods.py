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
FONTES_ROUNDS = 3
# A full round robin is the useful opening for a compact field.  Above this
# size it consumes too much of the event and init fontes gives a broader mix.
MAX_AUTO_ROUND_ROBIN_ENTRANTS = 14


class PairingMethod(StrEnum):
    SWISS_CONTENDERS = "SwissContenders"

    @property
    def label(self) -> str:
        return {self.SWISS_CONTENDERS: "Swiss Contenders"}[self]


class SwissContendersOpening(StrEnum):
    AUTO = "Auto"
    FONTES = "Fontes"
    ROUND_ROBIN = "RoundRobin"

    @property
    def label(self) -> str:
        return {
            self.AUTO: "Automatic",
            self.FONTES: "Init fontes",
            self.ROUND_ROBIN: "Full round robin",
        }[self]


@dataclass(frozen=True)
class PairingMethodSchedule:
    """The editable blocks produced by a schedule-level pairing method."""

    method: PairingMethod
    opening: SwissContendersOpening
    blocks: list[dict]


def _block(pairing: RP, rounds: int) -> dict:
    return {"pairing": str(pairing), "rounds": rounds, "pair_from": 1}


def _round_robin_rounds(entrants: int) -> int:
    # An odd field needs the ghost-player round so everyone receives one bye.
    return entrants - 1 if entrants % 2 == 0 else entrants


def _resolve_opening(
    opening: SwissContendersOpening, *, entrants: int
) -> SwissContendersOpening:
    if opening != SwissContendersOpening.AUTO:
        return opening
    if entrants <= MAX_AUTO_ROUND_ROBIN_ENTRANTS:
        return SwissContendersOpening.ROUND_ROBIN
    return SwissContendersOpening.FONTES


def swiss_contenders_schedule(
    *,
    entrants: int,
    total_rounds: int,
    opening: SwissContendersOpening = SwissContendersOpening.AUTO,
) -> PairingMethodSchedule:
    """Build CoCo's Swiss Contenders schedule.

    The standard form is three init-fontes rounds, strict no-repeat Swiss
    through ``floor(total_rounds / 2)``, then COP.  A compact field may instead
    begin with a complete round robin; because that exhausts every opponent,
    the remaining rounds move directly to COP rather than claiming that a
    repeat-free Swiss phase is possible.
    """

    if total_rounds < MIN_SWISS_CONTENDERS_ROUNDS:
        raise ValueError(
            f"Swiss Contenders requires at least {MIN_SWISS_CONTENDERS_ROUNDS} rounds."
        )
    if entrants < 2:
        raise ValueError("Swiss Contenders requires at least two entrants.")

    resolved = _resolve_opening(opening, entrants=entrants)
    if resolved == SwissContendersOpening.ROUND_ROBIN:
        round_robin_rounds = _round_robin_rounds(entrants)
        cop_rounds = total_rounds - round_robin_rounds
        if cop_rounds < 1:
            raise ValueError(
                "The event needs at least one round after the full round robin "
                "for the COP phase."
            )
        blocks = [
            _block(RP.RoundRobin, round_robin_rounds),
            _block(RP.COP, cop_rounds),
        ]
    else:
        if entrants < 4:
            raise ValueError("An init-fontes opening requires at least four entrants.")
        first_half_rounds = total_rounds // 2
        swiss_rounds = first_half_rounds - FONTES_ROUNDS
        cop_rounds = total_rounds - first_half_rounds
        blocks = [_block(RP.Quads_Equalized, FONTES_ROUNDS)]
        if swiss_rounds:
            blocks.append(_block(RP.SwissNoRepeats, swiss_rounds))
        blocks.append(_block(RP.COP, cop_rounds))

    return PairingMethodSchedule(
        method=PairingMethod.SWISS_CONTENDERS,
        opening=resolved,
        blocks=blocks,
    )


def pairing_method_schedule(
    method: PairingMethod,
    *,
    entrants: int,
    total_rounds: int,
    opening: SwissContendersOpening = SwissContendersOpening.AUTO,
) -> PairingMethodSchedule:
    """Dispatch a first-class pairing method to its schedule builder."""

    if method == PairingMethod.SWISS_CONTENDERS:
        return swiss_contenders_schedule(
            entrants=entrants,
            total_rounds=total_rounds,
            opening=opening,
        )
    raise ValueError(f"Unsupported pairing method: {method}")
