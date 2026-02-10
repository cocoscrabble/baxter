from collections import defaultdict
from random import random

from tournaments.pairing.base import (
    RP,
    RoundStatus,
    RoundPairing,
    Pairing,
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


def can_pair(rp, status):
    stat = status[rp.round]
    if stat == RoundStatus.Finished:
        return False
    if RP.is_round_robin(rp.pairing):
        # Round robins do not depend on results from a previous round
        return True
    else:
        return rp.start_round == 0 or status[rp.start_round] == RoundStatus.Finished


def pair_round(rp, pairing_data):
    strategy = STRATEGIES.get(rp.pairing)
    if strategy:
        return strategy(rp, pairing_data)
    else:
        return []


def extract_pairings(result_slips, round):
    res = result_slips.filter(round=round)
    return [(r.winner_name, r.loser_name) for r in res]


def _p1_starts(s1, s2):
    if s1 == s2:
        return random() > 0.5
    else:
        return s1 < s2


def pair(pd, config):
    """Pair a whole tournament round by round."""

    def p1_starts(p1, p2, rp):
        """Figure out who starts."""
        if p1.is_bye:
            # Always assign 'bye' the first otherwise the player playing the bye
            # is assigned an extra first and thereby slightly penalised.
            return True
        elif p2.is_bye:
            return False
        elif RP.is_round_robin(rp.pairing):
            # Round robins balance starts independently
            ret = _p1_starts(rr_starts[p1.name], rr_starts[p2.name])
            if ret:
                rr_starts[p1.name] += 1
            else:
                rr_starts[p2.name] += 1
            return ret
        else:
            return _p1_starts(p1.starts, p2.starts)

    ret = []
    rr_starts = defaultdict(lambda: 0)
    status = round_status(pd.result_slips, pd.entrants)
    round_pairings = [RoundPairing.from_dict(x) for x in config.round_pairings]
    for rp in round_pairings:
        if status[rp.round] == RoundStatus.Finished:
            # Add the played games to the repeats table
            for name1, name2 in extract_pairings(pd.result_slips, rp.round):
                pd.repeats.add(name1, name2)
        else:
            if can_pair(rp, status):
                pairings = []
                for p1, p2 in pair_round(rp, pd):
                    reps = pd.repeats.add(p1.name, p2.name)
                    if p1_starts(p1, p2, rp):
                        pairings.append(Pairing(p1, p2, reps))
                    else:
                        pairings.append(Pairing(p2, p1, reps))
                ret.append((rp.round, pairings))
    return ret


_STRATEGIES = [
    (RP.KotH, pair_koth),
    (RP.QotH, pair_qoth),
    (RP.Swiss, pair_swiss),
    (RP.RoundRobin, pair_round_robin),
    (RP.Quads_Clustered, pair_clustered_quads),
    (RP.Quads_Distributed, pair_distributed_quads),
    (RP.Quads_Evans, pair_evans_quads),
    (RP.Charlottesville, pair_charlottesville),
]
STRATEGIES = {k.name: v for (k, v) in _STRATEGIES}
STRATEGY_TYPES = list(STRATEGIES.keys())
