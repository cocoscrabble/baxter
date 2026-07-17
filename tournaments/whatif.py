"""What-if pairing exploration.

``explore_pairing`` re-pairs a single round hypothetically off a chosen based-on
round, as a pure function of the division's existing results — no DB writes. It
works on any editable division, not just imported ones. ``decorate`` turns the
engine's pairings into lightweight view rows (board order, repeat detail, and the
real score if the hypothetical pairing actually happened).
"""

from collections import defaultdict
from dataclasses import dataclass

from tournaments.pairing.base import PairingData, standings_after_round
from tournaments.pairing.engine import pair_with_engine
from tournaments.pairing.round_pairing import RoundPairing

_BYE = "bye"


def _is_bye(name: str) -> bool:
    return name.lower() == _BYE


@dataclass(frozen=True)
class ExploreRow:
    """One hypothetical pairing, decorated for display."""

    table: int | None      # board number; None for a bye row
    first: str
    second: str
    repeat_rounds: tuple[int, ...]  # earlier rounds (≤ based_on) this pair met
    result: str            # real score if this pairing actually happened, else ""


def explore_pairing(division, target_round, strategy, based_on, seed):
    """Pair ``target_round`` with ``strategy`` off the standings as of
    ``based_on`` (0 = seedings). Returns ``[DisplayPairing, ...]``.

    Truncating results to ``round <= based_on`` makes the engine treat the target
    round as unplayed and fixes the based-on standings; fixed and published
    pairings are cleared so the output is the pure strategy result. Raises
    ``PairingError`` if the round can't be paired. No DB writes.
    """
    pd = PairingData.for_division(division)
    pd.result_slips = [s for s in pd.result_slips if s.round <= based_on]
    pd.round_pairings = [RoundPairing(target_round, based_on, str(strategy))]
    pd.fixed_pairings = {}
    pd.published_pairings = {}
    pd.seed = seed
    for rnd, pairings in pair_with_engine(pd):
        if rnd == target_round:
            return pairings
    return []


def decorate(division, target_round, based_on, pairings):
    """Decorate engine ``pairings`` into ``[ExploreRow]``: board order by min
    standings rank (byes last), the repeat rounds through ``based_on``, and the
    actual score when the hypothetical pairing really happened in the target
    round."""
    pd = PairingData.for_division(division)
    # standings_after_round already counts only rounds ≤ based_on, so the full pd
    # gives the based-on ranking (seedings when based_on == 0).
    rank = {p.name: i for i, p in enumerate(standings_after_round(pd, based_on))}

    meetings: dict[frozenset, list[int]] = defaultdict(list)
    actual = {}
    for s in pd.result_slips:
        pair = frozenset({s.winner_name, s.loser_name})
        if s.round <= based_on and not (_is_bye(s.winner_name) or _is_bye(s.loser_name)):
            meetings[pair].append(s.round)
        if s.round == target_round:
            actual[pair] = s

    def score(slip, name):
        return slip.winner_score if slip.winner_name == name else slip.loser_score

    real, byes = [], []
    for p in pairings:
        first, second = p.first.name, p.second.name
        pair = frozenset({first, second})
        if _is_bye(first) or _is_bye(second):
            byes.append((first, second))
            continue
        slip = actual.get(pair)
        result = f"{score(slip, first)} - {score(slip, second)}" if slip else ""
        repeat_rounds = tuple(sorted(meetings.get(pair, [])))
        real.append((first, second, repeat_rounds, result))

    real.sort(key=lambda r: min(rank.get(r[0], 10**9), rank.get(r[1], 10**9)))
    rows = [
        ExploreRow(table=i, first=f, second=s, repeat_rounds=rr, result=res)
        for i, (f, s, rr, res) in enumerate(real, start=1)
    ]
    for first, second in byes:
        if _is_bye(first):  # show the real player first
            first, second = second, first
        rows.append(ExploreRow(table=None, first=first, second=second, repeat_rounds=(), result=""))
    return rows
