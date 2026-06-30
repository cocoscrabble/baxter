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
from tournaments.pairing.round_pairing import RP
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
    pair_equalized_quads,
    pair_sixes,
)
from tournaments.pairing.swiss import pair_swiss, pair_swiss_plus_random


# Name of the synthetic bye opponent. Matches Player.is_bye (name == "bye"),
# so the engine's start-balancing and bye bookkeeping recognise it.
BYE_NAME = "Bye"


def _byes_so_far(pd: PairingData) -> dict[str, int]:
    """Count how many byes each player has already received, from result history."""
    byes: dict[str, int] = defaultdict(int)
    for slip in pd.result_slips:
        if slip.winner_name.lower() == BYE_NAME.lower():
            byes[slip.loser_name] += 1
        elif slip.loser_name.lower() == BYE_NAME.lower():
            byes[slip.winner_name] += 1
    return byes


def bye_pairing(pd: PairingData, rp, fixed_pairs) -> tuple[str, str] | None:
    """Return a (player, "Bye") pair to force when the field is odd, else None.

    The bye goes to the lowest-ranked player who has had the fewest byes so far
    (rotating, so nobody gets a second bye until everyone has had one). Players
    already in a fixed pairing this round are not eligible. Round-robin and quad
    strategies have their own (not-yet-implemented) odd-field handling and are
    skipped here.
    """
    if RP.is_round_robin(rp.pairing) or RP.is_quad(rp.pairing):
        return None
    field = standings_after_round(pd, rp.start_round)
    fixed_names = {name for pair in fixed_pairs for name in pair}
    eligible = [p for p in field if p.name not in fixed_names]
    if len(eligible) % 2 == 0:
        return None
    byes = _byes_so_far(pd)
    fewest = min(byes[p.name] for p in eligible)
    # Lowest-ranked (standings run best-first) among those with the fewest byes.
    for p in reversed(eligible):
        if byes[p.name] == fewest:
            return (p.name, BYE_NAME)
    return (eligible[-1].name, BYE_NAME)


def can_pair(rp, status) -> bool:
    stat = status[rp.round]
    if stat in (RoundStatus.Finished, RoundStatus.Partial):
        return False
    if RP.is_round_robin(rp.pairing):
        # Round robins do not depend on results from a previous round
        return True
    else:
        return rp.start_round == 0 or status[rp.start_round] == RoundStatus.Finished


# Round-robin family that honors fixed pairings by permuting which round template
# lands in which round (see basic._rr_block_pairings), rather than the
# exclude-and-pair-the-rest path below — removing players from the rotation would
# corrupt the schedule.
_ROUND_ROBIN_FAMILY = {RP.RoundRobin, RP.DoubleRoundRobin}


def pair_round(pd: PairingData, rp) -> Pairings:
    if rp.pairing in _ROUND_ROBIN_FAMILY:
        # The strategy reads pd.fixed_pairings itself and permutes the rounds; it
        # must see the full field, so skip the exclude/append mechanism entirely.
        strategy = STRATEGIES.get(rp.pairing)
        return strategy(pd, rp) if strategy else Pairings()

    fixed_pairs = list(pd.fixed_pairings.get(rp.round, []))

    # Make an odd field even by forcing a bye for the chosen player. Treated as
    # just another fixed pairing, so the strategy only ever sees an even subset.
    bye = bye_pairing(pd, rp, fixed_pairs)
    if bye is not None:
        fixed_pairs.append(bye)

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
    # Count real entrants only; an odd field gets a bye, which adds one more game
    # (the bye result), so the number of games is ceil(real / 2). The persisted
    # bye entrant, if any, is excluded here.
    n_real = sum(1 for e in pd.entrants if e.player.name.lower() != BYE_NAME.lower())
    n_games = (n_real + 1) // 2
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


def pair(pd: PairingData) -> list[tuple[int, list[DisplayPairing]]]:
    """Pair a whole tournament round by round."""
    ret = []
    starts = Starts()
    status = round_status(pd)
    for rp in pd.round_pairings:
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
    RP.Quads_Equalized: pair_equalized_quads,
    RP.Sixes: pair_sixes,
    RP.Charlottesville: pair_charlottesville,
    RP.SwissPlusRandom: pair_swiss_plus_random,
}
STRATEGY_TYPES = list(STRATEGIES.keys())
