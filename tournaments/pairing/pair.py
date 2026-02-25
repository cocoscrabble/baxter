from collections import defaultdict

from tournaments.pairing.base import (
    DisplayPairing,
    PairingData,
    Pairings,
    Player,
    RoundStatus,
    Starts,
    standings_after_round,
)
from tournaments.pairing.round_pairing import RP, RoundPairing
from tournaments.pairing.basic import (
    pair_koth,
    pair_qoth,
    pair_random,
    pair_random_no_repeats,
    pair_round_robin,
    pair_double_round_robin,
    pair_charlottesville,
)
from tournaments.pairing.quads import (
    pair_clustered_quads,
    pair_distributed_quads,
    pair_evans_quads,
    pair_sixes,
)
from tournaments.pairing.swiss import pair_swiss, pair_swiss_plus_random


def can_pair(rp, status) -> bool:
    stat = status[rp.round]
    if stat in (RoundStatus.Finished, RoundStatus.Partial):
        return False
    if RP.is_round_robin(rp.pairing):
        # Round robins do not depend on results from a previous round
        return True
    else:
        return rp.start_round == 0 or status[rp.start_round] == RoundStatus.Finished


def pair_round(pd: PairingData, rp) -> Pairings:
    fixed_pairs = pd.fixed_pairings.get(rp.round, [])

    if fixed_pairs:
        # Temporarily exclude fixed players from standings so the strategy only sees
        # the remaining entrants. See PairingData.excluded_names for full explanation.
        pd.excluded_names = {name for pair in fixed_pairs for name in pair}

    strategy = STRATEGIES.get(rp.pairing)
    result = strategy(pd, rp) if strategy else Pairings()

    pd.excluded_names = set()

    if fixed_pairs:
        # Look up Player objects from the full (unfiltered) standings so that starts.add()
        # has accurate score/starts data for the starts-balancing decision.
        all_players = {p.name: p for p in standings_after_round(pd, rp.start_round)}
        for name1, name2 in fixed_pairs:
            p1 = all_players.get(name1) or Player(name1)
            p2 = all_players.get(name2) or Player(name2)
            result.add(p1, p2)

    return result


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
    RP.DoubleRoundRobin: pair_double_round_robin,
    RP.Random: pair_random,
    RP.RandomNoRepeats: pair_random_no_repeats,
    RP.Quads_Clustered: pair_clustered_quads,
    RP.Quads_Distributed: pair_distributed_quads,
    RP.Quads_Evans: pair_evans_quads,
    RP.Sixes: pair_sixes,
    RP.Charlottesville: pair_charlottesville,
    RP.SwissPlusRandom: pair_swiss_plus_random,
}
STRATEGY_TYPES = list(STRATEGIES.keys())
