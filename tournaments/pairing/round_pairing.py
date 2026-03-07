from dataclasses import dataclass
from enum import StrEnum

from dataclasses_json import dataclass_json


class RP(StrEnum):
    KotH = "KotH"
    QotH = "QotH"
    Swiss = "Swiss"
    RoundRobin = "RoundRobin"
    DoubleRoundRobin = "DoubleRoundRobin"
    Random = "Random"
    RandomNoRepeats = "RandomNoRepeats"
    Quads_Clustered = "Quads_Clustered"
    Quads_Distributed = "Quads_Distributed"
    Quads_Equalized = "Quads_Equalized"
    Sixes = "Sixes"
    Charlottesville = "Charlottesville"
    SwissPlusRandom = "SwissPlusRandom"

    @staticmethod
    def is_round_robin(name) -> bool:
        return name in (RP.RoundRobin, RP.DoubleRoundRobin, RP.Charlottesville)

    @staticmethod
    def is_quad(name) -> bool:
        return name in (RP.Quads_Clustered, RP.Quads_Distributed, RP.Quads_Equalized, RP.Sixes)


ABBREV = {
    "KH": RP.KotH,
    "QH": RP.QotH,
    "SW": RP.Swiss,
    "RR": RP.RoundRobin,
    "DR": RP.DoubleRoundRobin,
    "RA": RP.Random,
    "RN": RP.RandomNoRepeats,
    "QC": RP.Quads_Clustered,
    "QD": RP.Quads_Distributed,
    "QE": RP.Quads_Equalized,
    "SX": RP.Sixes,
    "SR": RP.SwissPlusRandom,
}


@dataclass_json
@dataclass
class RoundPairing:
    round: int
    start_round: int
    pairing: str


def make_pairings(spec: str) -> list[RoundPairing]:
    out = []
    parts = spec.split(" ")
    for p in parts:
        p = p.upper()
        if ":" in p:
            k, v = p.split(":")
            v = int(v)
        else:
            k, v = p, 1
        rp = ABBREV[k]
        curr = len(out) + 1
        for i in range(v):
            if RP.is_round_robin(rp):
                # All entries in a single round robin share the first round of
                # the set as their start_round (which is itself part of the block).
                e = RoundPairing(curr + i, curr, rp)
            elif RP.is_quad(rp):
                # All entries in a quad/sixes block share the round immediately
                # before the block as their start_round (not part of the block).
                e = RoundPairing(curr + i, curr - 1, rp)
            else:
                # Otherwise, each round is paired from the previous one.
                e = RoundPairing(curr + i, curr + i - 1, rp)
            out.append(e)
    return out
