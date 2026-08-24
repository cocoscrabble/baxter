"""What-if pairing exploration.

``explore_pairing`` re-pairs a single round hypothetically off a chosen based-on
round, as a pure function of the division's existing results — no DB writes. It
works on any editable division, not just imported ones. ``decorate`` turns the
engine's pairings into lightweight view rows (board order, repeat detail, and the
real score if the hypothetical pairing actually happened); ``actual_rows`` builds
the same rows from the round's real result, for the side-by-side comparison.
"""

import copy
from collections import defaultdict
from dataclasses import dataclass, replace

from tournaments.pairing.base import PairingData, standings_after_round
from tournaments.pairing.engine import pair_with_engine
from tournaments.pairing.round_pairing import RoundPairing

# The bye's reserved player number (BYE_PLAYER_NUMBER), casefolded for the
# comparison below. Pairings key on the number now, so this is what a bye looks
# like everywhere in this module.
_BYE = "bye"


def _is_bye(key: str) -> bool:
    return key.casefold() == _BYE


@dataclass(frozen=True)
class ExploreRow:
    """One pairing (hypothetical or actual), decorated for display.

    ``first``/``second`` are display names; ``first_key``/``second_key`` are the
    identities, which is what ``mark_common`` matches on — two rows are the same
    game when the same *players* meet, not when the same names do.
    """

    table: int | None      # board number; None for a bye row
    first: str
    first_record: str      # "3-2 (+235)" as of the pairing's basis round
    second: str
    second_record: str
    repeat_rounds: tuple[int, ...]  # earlier rounds this pair had already met
    # Required, not defaulted: a row built without its keys would compare equal
    # to every other such row in mark_common, silently marking everything common.
    first_key: str
    second_key: str
    common: bool = False   # this pair appears on both the what-if and actual side


def explore_pairing(division, target_round, strategy, based_on, seed, pd=None):
    """Pair ``target_round`` with ``strategy`` off the standings as of
    ``based_on`` (0 = seedings). Returns ``[DisplayPairing, ...]``.

    Truncating results to ``round <= based_on`` makes the engine treat the target
    round as unplayed and fixes the based-on standings; fixed and published
    pairings are cleared so the output is the pure strategy result. Raises
    ``PairingError`` if the round can't be paired. No DB writes.

    Pass a prebuilt ``pd`` (``PairingData.for_division``) to share it with
    ``decorate``/``actual_rows`` and avoid rebuilding it; it is shallow-copied
    before its slips are truncated, so the caller's ``pd`` is left untouched.
    """
    pd = copy.copy(pd) if pd is not None else PairingData.for_division(division)
    pd.result_slips = [s for s in pd.result_slips if s.round <= based_on]
    pd.round_pairings = [RoundPairing(target_round, based_on, str(strategy))]
    pd.fixed_pairings = {}
    pd.published_pairings = {}
    pd.seed = seed
    for rnd, pairings in pair_with_engine(pd):
        if rnd == target_round:
            return pairings
    return []


def _meeting_rounds(pd, upto):
    """{pair -> sorted rounds ≤ upto they met}, byes excluded."""
    meetings: dict[frozenset, list[int]] = defaultdict(list)
    for s in pd.result_slips:
        if s.round <= upto and not (_is_bye(s.winner_key) or _is_bye(s.loser_key)):
            meetings[frozenset({s.winner_key, s.loser_key})].append(s.round)
    return {pair: tuple(sorted(rounds)) for pair, rounds in meetings.items()}


def _standings_info(pd, upto):
    """``(rank, record, name)`` as of round ``upto`` (seedings at 0), each keyed
    on the player key: board rank, "W-L (±spread)" for the record column, and the
    display name."""
    standings = standings_after_round(pd, upto)
    rank = {p.key: i for i, p in enumerate(standings)}
    record = {p.key: f"{p.record} ({p.spread:+d})" for p in standings}
    names = {p.key: p.name for p in standings}
    return rank, record, names


def _order(real, rank, record, names):
    """Board-order real ``(first, second, repeat_rounds)`` key-tuples by min
    standings rank, number them 1..n, and attach each player's name and record."""
    real.sort(key=lambda r: min(rank.get(r[0], 10**9), rank.get(r[1], 10**9)))
    return [
        ExploreRow(
            table=i,
            first=names.get(f, f), first_record=record.get(f, ""),
            second=names.get(s, s), second_record=record.get(s, ""),
            repeat_rounds=rr, first_key=f, second_key=s,
        )
        for i, (f, s, rr) in enumerate(real, start=1)
    ]


def _bye_rows(byes, record, names):
    """Bye rows (real player first, no board), carrying the real player's record."""
    rows = []
    for first, second in byes:
        if _is_bye(first):
            first, second = second, first
        rows.append(ExploreRow(
            table=None,
            first=names.get(first, first), first_record=record.get(first, ""),
            second=names.get(second, second), second_record="",
            repeat_rounds=(), first_key=first, second_key=second,
        ))
    return rows


def decorate(division, target_round, based_on, pairings, pd=None):
    """Decorate engine ``pairings`` into ``[ExploreRow]``: board order by min
    standings rank (byes last), each player's record+spread as of ``based_on``,
    and the repeat rounds through it."""
    pd = pd or PairingData.for_division(division)
    rank, record, names = _standings_info(pd, based_on)
    meetings = _meeting_rounds(pd, based_on)

    real, byes = [], []
    for p in pairings:
        first, second = p.first.key, p.second.key
        if _is_bye(first) or _is_bye(second):
            byes.append((first, second))
            continue
        pair = frozenset({first, second})
        real.append((first, second, meetings.get(pair, ())))

    return _order(real, rank, record, names) + _bye_rows(byes, record, names)


def actual_rows(division, target_round, pd=None):
    """The real pairings of ``target_round`` as ``[ExploreRow]``, or ``None`` if
    the round wasn't played — for the side-by-side comparison. Records are as of
    the round before; board order is by the standings going into it."""
    pd = pd or PairingData.for_division(division)
    slips = [s for s in pd.result_slips if s.round == target_round]
    if not slips:
        return None
    rank, record, names = _standings_info(pd, target_round - 1)
    meetings = _meeting_rounds(pd, target_round - 1)

    real, byes = [], []
    for s in slips:
        first, second = s.first_key, s.second_key
        if _is_bye(first) or _is_bye(second):
            byes.append((first, second))
            continue
        pair = frozenset({first, second})
        real.append((first, second, meetings.get(pair, ())))

    return _order(real, rank, record, names) + _bye_rows(byes, record, names)


def configured_pairing(division, round_num, pd=None):
    """The ``RoundPairing`` the division has configured for ``round_num`` — its
    strategy and basis round, shown on the Actual side so it reads like the
    what-if descriptor — or ``None`` if the round isn't configured."""
    pd = pd or PairingData.for_division(division)
    for rp in pd.round_pairings:
        if rp.round == round_num:
            return rp
    return None


def mark_common(whatif, actual):
    """Set ``common=True`` on the rows of each side whose pair appears on both,
    returning ``(whatif, actual)``. ``actual`` may be ``None``."""
    if not actual:
        return whatif, actual
    def pair(r):
        return frozenset({r.first_key, r.second_key})

    whatif_pairs = {pair(r) for r in whatif}
    actual_pairs = {pair(r) for r in actual}
    whatif = [replace(r, common=pair(r) in actual_pairs) for r in whatif]
    actual = [replace(r, common=pair(r) in whatif_pairs) for r in actual]
    return whatif, actual
