"""Refresh WESPA ratings on the global player roster.

``Player.wespa_rating`` is consulted only when a player has no CoCo rating
(``Player.effective_rating``), and it never syncs to the central database — it is
Baxter's own copy of somebody else's number.

**The fetcher is deliberately absent.** Where these ratings come from is not
settled: a bulk file, a per-player lookup, some URL, some format. Inventing one
would be guessing, so this module takes rows that are already parsed and leaves
acquiring them to whoever knows (plans/PLAN_ENTRANTS.md decision 11). The
admin-only upload page is the concrete way in for now.

Matching follows the same rule as the entrant CSV import, for the same reason:

- a row with a ``player_number`` resolves exactly;
- a row with only a name resolves if that name belongs to exactly one player;
- a name shared by several players updates **none** of them and is reported,
  because WESPA has no idea which "John Smith" it means and a wrong rating is
  worse than a missing one.

Refreshing ratings mutates no replayable tournament state — entrants pinned
theirs at entry (decision 3) — so this stays an unlogged global action, like the
roster import it is modelled on.
"""

import csv
import io
from dataclasses import dataclass, field

from coco_ratings.identity import canonical_player_number
from django.db import transaction

from .models import Player

HEADERS = ("player_number", "name", "wespa_rating")


@dataclass
class WespaRefreshResult:
    updated: list = field(default_factory=list)   # player names
    unchanged: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)  # names/numbers not found
    ambiguous: list = field(default_factory=list)  # names matching several

    @property
    def total(self):
        return (
            len(self.updated) + len(self.unchanged)
            + len(self.unmatched) + len(self.ambiguous)
        )


def parse_wespa_csv(text):
    """Parse ``[player_number,] name, wespa_rating`` rows.

    Two shapes, like the entrant import: two columns are ``name, rating``, three
    are ``number, name, rating``. A header row naming the columns is skipped if
    present, so a file exported with headings does not need editing first.

    Returns ``(rows, errors)`` where each row is ``(number, name, rating)``.
    """
    reader = csv.reader(io.StringIO(text))
    raw = [r for r in reader if any(cell.strip() for cell in r)]
    if not raw:
        return [], ["The file is empty."]

    first = [cell.strip().lower().replace(" ", "_") for cell in raw[0]]
    if any(h in HEADERS for h in first):
        raw = raw[1:]

    rows, errors = [], []
    for i, row in enumerate(raw, start=1):
        cells = [cell.strip() for cell in row]
        if len(cells) == 2:
            number, name, rating_s = "", cells[0], cells[1]
        elif len(cells) == 3:
            number, name, rating_s = cells
        else:
            errors.append(f"Row {i}: expected 2 or 3 columns, got {len(cells)}.")
            continue
        if not name and not number:
            errors.append(f"Row {i}: a name or a player number is required.")
            continue
        try:
            rating = int(rating_s)
        except ValueError:
            errors.append(f"Row {i}: invalid rating {rating_s!r}.")
            continue
        rows.append((canonical_player_number(number) if number else "", name, rating))
    return rows, errors


def refresh_wespa_ratings(rows):
    """Apply parsed ``(number, name, rating)`` rows. Returns a result summary.

    All-or-nothing per row rather than per file: a name that cannot be resolved
    is skipped and reported while the rest apply, because a roster refresh is
    routine maintenance and one unknown player should not block the other
    hundred.
    """
    result = WespaRefreshResult()
    by_number = {}
    by_name = {}
    for player in Player.objects.filter(is_bye=False):
        by_number[player.player_number] = player
        by_name.setdefault(player.name.casefold(), []).append(player)

    to_update = []
    for number, name, rating in rows:
        if number:
            player = by_number.get(number)
            if player is None:
                result.unmatched.append(number)
                continue
        else:
            candidates = by_name.get(name.casefold(), [])
            if not candidates:
                result.unmatched.append(name)
                continue
            if len(candidates) > 1:
                listed = ", ".join(f"#{p.player_number}" for p in candidates)
                result.ambiguous.append(f"{name} ({listed})")
                continue
            player = candidates[0]

        if player.wespa_rating == rating:
            result.unchanged.append(player.name)
            continue
        player.wespa_rating = rating
        to_update.append(player)
        result.updated.append(player.name)

    if to_update:
        with transaction.atomic():
            Player.objects.bulk_update(to_update, ["wespa_rating"], batch_size=500)
    return result
