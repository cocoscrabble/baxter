from tournaments.pairing.base import (
    RP,
    DisplayPairing,
    PairingData,
    Pairings,
    RoundPairing,
    RoundStatus,
    Starts,
    round_status,
)
from tournaments.pairing.basic import (
    pair_koth,
    pair_qoth,
    pair_round_robin,
    pair_charlottesville,
)
from tournaments.pairing.quads import (
    pair_clustered_quads,
    pair_distributed_quads,
    pair_evans_quads,
)
from tournaments.pairing.swiss import pair_swiss


def can_pair(rp, status) -> bool:
    stat = status[rp.round]
    if stat == RoundStatus.Finished:
        return False
    if RP.is_round_robin(rp.pairing):
        # Round robins do not depend on results from a previous round
        return True
    else:
        return rp.start_round == 0 or status[rp.start_round] == RoundStatus.Finished


def pair_round(pd: PairingData, rp) -> Pairings:
    strategy = STRATEGIES.get(rp.pairing)
    if strategy:
        return strategy(pd, rp)
    else:
        return Pairings()


def extract_pairings(pd: PairingData, round: int) -> Pairings:
    """Return pairings with starter first for each result in a round."""
    res = pd.result_slips.filter(round=round)
    pairings = Pairings()
    for r in res:
        pairings.add_result_slip(r)
    return pairings


def pair(pd: PairingData, config) -> list[tuple[int, list[DisplayPairing]]]:
    """Pair a whole tournament round by round."""
    ret = []
    starts = Starts()
    status = round_status(pd)
    round_pairings = [RoundPairing.from_dict(x) for x in config.round_pairings]
    for rp in round_pairings:
        if status[rp.round] == RoundStatus.Finished:
            for first, second in extract_pairings(pd, rp.round):
                pd.repeats.add(first, second)
                starts.register(first, second, rp.round)
        else:
            if can_pair(rp, status):
                pairings = []
                for p1, p2 in pair_round(pd, rp):
                    reps = pd.repeats.add(p1, p2)
                    first, second = starts.add(p1, p2, rp.round)
                    pairings.append(DisplayPairing(first, second, reps))
                ret.append((rp.round, pairings))
    return ret


STRATEGIES = {
    RP.KotH: pair_koth,
    RP.QotH: pair_qoth,
    RP.Swiss: pair_swiss,
    RP.RoundRobin: pair_round_robin,
    RP.Quads_Clustered: pair_clustered_quads,
    RP.Quads_Distributed: pair_distributed_quads,
    RP.Quads_Evans: pair_evans_quads,
    RP.Charlottesville: pair_charlottesville,
}
STRATEGY_TYPES = list(STRATEGIES.keys())
