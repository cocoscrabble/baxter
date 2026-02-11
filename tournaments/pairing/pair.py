from collections import defaultdict

from tournaments.pairing.base import (
    RP,
    DisplayPairing,
    PairingData,
    Pairings,
    RoundPairing,
    RoundStatus,
    Starts,
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


def round_status(pd: PairingData) -> dict[int, RoundStatus]:
    counts = defaultdict(lambda: RoundStatus.Empty)
    n_games = len(pd.entrants) // 2
    round_counts = defaultdict(int)
    for slip in pd.result_slips:
        round_counts[slip.round] += 1
    for round, count in round_counts.items():
        if count == n_games:
            counts[round] = RoundStatus.Finished
        elif count > 0:
            counts[round] = RoundStatus.Partial
    return counts


def extract_pairings(pd: PairingData, round: int) -> Pairings:
    """Return pairings with starter first for each result in a round."""
    pairings = Pairings()
    for r in pd.result_slips:
        if r.round == round:
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
            for p in extract_pairings(pd, rp.round):
                pd.repeats.add(p)
                starts.register(p, rp.round)
        else:
            if can_pair(rp, status):
                pairings = []
                for p in pair_round(pd, rp):
                    reps = pd.repeats.add(p)
                    result = starts.add(p, rp.round)
                    pairings.append(DisplayPairing(result.first, result.second, reps))
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
