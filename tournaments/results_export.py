"""Export a division's results as CSV in the pdxwords / coco-ratings format.

Columns: ``Submitted On, Round, Winner, Winners Score, Opponent, Opponents
Score, Winner Number, Opponent Number`` — one row per game, ordered by round
then submission time. This is the shape the coco_ratings ``ResultCSVReader``
consumes (via csv_to_tou), so a file produced here feeds straight into the
ratings tooling.

The two ``Number`` columns are **additive**. A name is not an identity any more
(plans/PLAN_PLAYER_IDENTITY.md), so joining this file on names alone is
ambiguous the moment two players share one; the numbers make the join exact.
They cannot simply *replace* the names, because the same format is still
produced by a Google Form export that has no numbers to give — players type
their own names into it — so every reader has to keep accepting the six-column
form (../ratings/plans/baxter-integration.md phase 2).

**They are appended, not interleaved, and that ordering is load-bearing.**
``coco_ratings.io.ResultCSVReader.parse_row`` unpacks positionally::

    _time, round, winner, win_score, opp, opp_score, *rest = row

so a column inserted before ``Opponents Score`` shifts every field after it:
``win_score`` reads a player number and ``opp`` reads a score. On today's data
that fails loudly ("Score field contained a non-digit"), but only because the
misread cell happens not to look like a number. The trailing ``*rest`` is what
makes *appending* safe: the ratings reader ignores the new columns entirely, and
can start using them whenever it likes. Do not reorder these headers —
``test_results_export`` pins the order against the real reader.

Appending is also why the name columns are not disambiguated here the way the
web pages are: a machine-readable join key must not acquire a "(0233)" suffix.

The "Submitted On" cell is an Excel-style serial date (days since 1899-12-30,
fractional part = time of day). The ratings reader takes that column
positionally and ignores its value, but real spreadsheet exports carry a serial
there, so we reproduce one from each result's ``created_at``.

This module is deliberately free of Django imports so it can be exercised in
isolation; callers build a list of :class:`ResultRow` and hand it to
:func:`render_results_csv`, which returns the CSV text.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone

HEADERS = ["Submitted On", "Round", "Winner", "Winners Score",
           "Opponent", "Opponents Score", "Winner Number", "Opponent Number"]

# The pre-identity six-column form, still produced by the Google Form export and
# still valid input everywhere that reads this format. Readers dispatch on the
# header width; nothing writes it any more.
LEGACY_HEADERS = ["Submitted On", "Round", "Winner", "Winners Score",
                  "Opponent", "Opponents Score"]

# Excel's serial-date epoch: it counts from 1899-12-30 (a quirk of treating
# 1900 as a leap year), so this date is serial 0.
_EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ResultRow:
    """One game from the winner's point of view."""

    round: int
    winner: str
    winner_score: int
    opponent: str
    opponent_score: int
    submitted_on: datetime | None = None
    # Player numbers. Empty only for a row assembled from a source that has
    # none — a historical import, say — never for a Baxter export.
    winner_number: str = ""
    opponent_number: str = ""


def _excel_serial(dt):
    """Render ``dt`` as an Excel serial date, or "" when unknown."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (dt.astimezone(timezone.utc) - _EXCEL_EPOCH).total_seconds() / 86400
    return f"{days:.5f}"


def render_results_csv(rows):
    """Render ``rows`` (an iterable of :class:`ResultRow`) as CSV text."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow([
            _excel_serial(row.submitted_on),
            row.round,
            row.winner,
            row.winner_score,
            row.opponent,
            row.opponent_score,
            row.winner_number,
            row.opponent_number,
        ])
    return buf.getvalue()
