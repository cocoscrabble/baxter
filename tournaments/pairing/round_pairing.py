from dataclasses import dataclass
from enum import StrEnum

from dataclasses_json import dataclass_json


class RP(StrEnum):
    KotH = "KotH"
    QotH = "QotH"
    Swiss = "Swiss"
    RoundRobin = "RoundRobin"
    Quads_Clustered = "Quads_Clustered"
    Quads_Distributed = "Quads_Distributed"
    Quads_Evans = "Quads_Evans"
    Charlottesville = "Charlottesville"

    @staticmethod
    def is_round_robin(name) -> bool:
        return name in (RP.RoundRobin, RP.Charlottesville)


ABBREV = {
    "KH": RP.KotH,
    "QH": RP.QotH,
    "SW": RP.Swiss,
    "RR": RP.RoundRobin,
    "QC": RP.Quads_Clustered,
    "QD": RP.Quads_Distributed,
    "QE": RP.Quads_Evans,
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
                # All entries in a single round robin have their start round as the
                # first round of the set
                e = RoundPairing(curr + i, curr, rp)
            else:
                # Otherwise, each round is paired from the previous one
                e = RoundPairing(curr + i, curr + i - 1, rp)
            out.append(e)
    return out
