"""Division statistics: the record games of the event so far.

Ported from the Google Sheets tournament script's "Stats" sheet
(``outputStatistics`` in ../googlesheets-tournament-script/Code.ts), which
listed the top ten of eight categories. Two departures, both fixes:

- Byes *and forfeits* are left out (``ResultSlipQuerySet.played``). The sheet
  only dropped a bye that lost, so a forfeit — the bye winning — counted as a
  game.
- A tie is not a win: the sheet listed tied games under the win columns while
  already leaving them out of the loss columns. Ties have their own list.

Equal values share a rank (1, 2, 2, 4); within them, earlier rounds come first.
"""

from dataclasses import dataclass

from tournaments.display import division_labels, label_entrants

STATS_LIMIT = 10


@dataclass
class StatRow:
    """One game, seen from the side the category is about."""

    rank: int
    round: int
    player: object  # Entrant, labelled with ``display_name``
    score: int
    opponent: object
    opponent_score: int
    # The category's own figure (a total, a spread) and what it is called, or
    # None when the scores already are the figure.
    value: int | None = None
    value_label: str = ""


@dataclass
class StatCategory:
    title: str
    rows: list[StatRow]


def _decisive(slip):
    return slip.winner_score != slip.loser_score


# title, value label, which games, sort key (smaller first), from whose side,
# and the value shown. The order is the sheet's: the four score lists, then the
# four game lists.
_CATEGORIES = [
    ("Highest wins", "", _decisive, lambda s: -s.winner_score, "winner", None),
    ("Highest losses", "", _decisive, lambda s: -s.loser_score, "loser", None),
    ("Lowest wins", "", _decisive, lambda s: s.winner_score, "winner", None),
    ("Lowest losses", "", _decisive, lambda s: s.loser_score, "loser", None),
    ("Highest games", "Total", lambda _: True,
     lambda s: -(s.winner_score + s.loser_score), "winner",
     lambda s: s.winner_score + s.loser_score),
    ("Biggest blowouts", "Spread", _decisive,
     lambda s: -(s.winner_score - s.loser_score), "winner",
     lambda s: s.winner_score - s.loser_score),
    ("Nailbiters", "Spread", _decisive,
     lambda s: s.winner_score - s.loser_score, "winner",
     lambda s: s.winner_score - s.loser_score),
    ("Ties", "", lambda s: not _decisive(s), lambda s: -s.winner_score, "winner", None),
]


def _row(slip, rank, side, value, value_label):
    if side == "winner":
        player, score, opponent, opponent_score = (
            slip.winner, slip.winner_score, slip.loser, slip.loser_score
        )
    else:
        player, score, opponent, opponent_score = (
            slip.loser, slip.loser_score, slip.winner, slip.winner_score
        )
    return StatRow(
        rank=rank,
        round=slip.round,
        player=player,
        score=score,
        opponent=opponent,
        opponent_score=opponent_score,
        value=value(slip) if value else None,
        value_label=value_label,
    )


def division_statistics(division, limit=STATS_LIMIT) -> list[StatCategory]:
    """The division's categories, each its top ``limit`` games. A category with
    no games (no ties yet, say) is left out."""
    slips = list(
        division.result_slips.played()
        .select_related("winner__player", "loser__player")
        .order_by("round", "pk")
    )
    label_entrants(
        division_labels(division), (s.winner for s in slips), (s.loser for s in slips)
    )
    categories = []
    for title, value_label, keep, key, side, value in _CATEGORIES:
        # Stable: equal keys stay in round order.
        games = sorted((s for s in slips if keep(s)), key=key)
        rows, rank = [], 0
        for i, slip in enumerate(games[:limit]):
            if i == 0 or key(slip) != key(games[i - 1]):
                rank = i + 1
            rows.append(_row(slip, rank, side, value, value_label))
        if rows:
            categories.append(StatCategory(title, rows))
    return categories
