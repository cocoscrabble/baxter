from dataclasses import dataclass
from enum import StrEnum

from dataclasses_json import dataclass_json


class RP(StrEnum):
    KotH = "KotH"
    QotH = "QotH"
    Swiss = "Swiss"
    SwissNoRepeats = "SwissNoRepeats"
    SwissMinRepeats = "SwissMinRepeats"
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
    COP = "COP"

    @staticmethod
    def is_round_robin(name) -> bool:
        return name in (RP.RoundRobin, RP.DoubleRoundRobin, RP.Charlottesville)

    @staticmethod
    def is_quad(name) -> bool:
        return name in (RP.Quads_Clustered, RP.Quads_Distributed, RP.Quads_Equalized, RP.Sixes)


# The pairing strategies the schedule editor offers. Every entry must be a name
# the Rust engine accepts (see test_rust_engine); the engine is now the only
# implementation.
STRATEGY_TYPES = [
    RP.KotH,
    RP.QotH,
    RP.Swiss,
    RP.SwissNoRepeats,
    RP.SwissMinRepeats,
    RP.RoundRobin,
    RP.DoubleRoundRobin,
    RP.Random,
    RP.RandomNoRepeats,
    RP.Quads_Clustered,
    RP.Quads_Distributed,
    RP.Quads_Equalized,
    RP.Sixes,
    RP.Charlottesville,
    RP.SwissPlusRandom,
    # COP pairs off the previous round like a sliding strategy; it additionally
    # needs DivisionSettings.cop_config (prizes + tuning) to pair.
    RP.COP,
]


ABBREV = {
    "KH": RP.KotH,
    "QH": RP.QotH,
    "SW": RP.Swiss,
    "SN": RP.SwissNoRepeats,
    "SM": RP.SwissMinRepeats,
    "RR": RP.RoundRobin,
    "DR": RP.DoubleRoundRobin,
    "RA": RP.Random,
    "RN": RP.RandomNoRepeats,
    "QC": RP.Quads_Clustered,
    "QD": RP.Quads_Distributed,
    "QE": RP.Quads_Equalized,
    "SX": RP.Sixes,
    "SR": RP.SwissPlusRandom,
    "CO": RP.COP,
}


@dataclass_json
@dataclass
class RoundPairing:
    round: int
    start_round: int
    pairing: str


def normalize_round_robin_start_rounds(rps: list[RoundPairing]) -> list[RoundPairing]:
    """Make each contiguous round-robin block share its first round as start_round.

    A round-robin schedule rotates off a single fixed ordering (the standings as
    of ``start_round``), so every round in the block must point at the same one —
    this is what ``make_pairings`` produces. The settings editor instead stores a
    per-round ``start_round`` (defaulting to ``round - 1``), which leaves later
    rounds reading results that don't exist yet and pairing nobody. Repair those
    blocks in place; non-round-robin rounds are left untouched.
    """
    i = 0
    while i < len(rps):
        if RP.is_round_robin(rps[i].pairing):
            block_pairing = rps[i].pairing
            block_start = rps[i].round
            while i < len(rps) and rps[i].pairing == block_pairing:
                rps[i].start_round = block_start
                i += 1
        else:
            i += 1
    return rps


def blocks_to_round_pairings(blocks) -> list[RoundPairing]:
    """Expand block specs into the per-round pairing list.

    Each block is ``{"pairing", "rounds", "pair_from"}``. Rounds are numbered
    cumulatively; ``pair_from = N`` sets each round's standings source by family:
      - sliding (Swiss/KotH/Random/...): ``start_round = round - N``
      - quads/sixes: one fixed snapshot, ``start_round = blockStart - N``
      - round-robin: ``start_round = blockStart`` (rotates off a fixed order)
    """
    out = []
    for block in blocks:
        pairing = block["pairing"]
        rounds = int(block.get("rounds") or 0)
        pair_from = int(block.get("pair_from") or 1)
        start = len(out) + 1
        for i in range(rounds):
            r = start + i
            if RP.is_round_robin(pairing):
                start_round = start
            elif RP.is_quad(pairing):
                start_round = start - pair_from
            else:
                start_round = r - pair_from
            out.append(RoundPairing(r, start_round, pairing))
    return out


def default_block_rounds(n_entrants: int) -> dict:
    """Per-strategy default round counts for a field of ``n_entrants`` (all
    editable afterward). Strategies not listed default to 1 on the client."""
    rr = (n_entrants - 1) if n_entrants % 2 == 0 else n_entrants
    rr = max(rr, 0)
    return {
        RP.RoundRobin: rr,
        RP.DoubleRoundRobin: 2 * rr,
        RP.Charlottesville: n_entrants // 2,
        RP.Quads_Clustered: 3,
        RP.Quads_Distributed: 3,
        RP.Quads_Equalized: 3,
        RP.Sixes: 3,
    }


def round_pairings_to_blocks(round_pairings) -> list[dict]:
    """Group a per-round list back into blocks (for seeding the editor from an
    existing schedule).

    Consecutive rounds join the same block only when they share a strategy AND
    the same ``pair_from``. Because that comparison differs by family — sliding
    strategies keep a constant per-round offset (``round - start_round``), while
    quads/round-robin pair off one fixed snapshot (constant ``start_round``) —
    each round gets a family-aware signature; a change in signature starts a new
    block. ``pair_from`` is taken from each block's first round.
    """
    blocks = []
    last_sig = None
    for rp in sorted(round_pairings, key=lambda x: x["round"]):
        pairing = rp["pairing"]
        if RP.is_round_robin(pairing):
            # Round-robin doesn't pair off standings, so pair_from is nominal —
            # consecutive RR rounds are one block regardless of stored start_round.
            sig = (pairing, "rr")
            pair_from = 1
        elif RP.is_quad(pairing):
            # Quads pair off one fixed snapshot; the block is delimited by that
            # snapshot (constant start_round within a block).
            sig = (pairing, "quad", rp["start_round"])
            pair_from = rp["round"] - rp["start_round"]  # blockStart - start_round
        else:
            offset = rp["round"] - rp["start_round"]
            sig = (pairing, "slide", offset)
            pair_from = offset
        if sig == last_sig:
            blocks[-1]["rounds"] += 1
            continue
        blocks.append({"pairing": pairing, "rounds": 1, "pair_from": pair_from})
        last_sig = sig
    return blocks


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
