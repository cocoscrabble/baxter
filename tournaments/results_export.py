"""Export a division's results as CSV in the pdxwords / coco-ratings format.

Columns: ``Submitted On, Round, Winner, Winners Score, Opponent, Opponents
Score`` — one row per game, ordered by round then submission time. This is the
shape the coco_ratings ``ResultCSVReader`` consumes (via csv_to_tou), so a file
produced here feeds straight into the ratings tooling.

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
        ])
    return buf.getvalue()
