"""Bulk import of entrants from CSV data.

Parses CSV rows, resolves or creates Players, and adds Entrants to a Division.

A row may name a player, or give their player number. The number is the
identity, so it resolves exactly; a bare name is a lookup that may turn out to
be ambiguous, and an ambiguous name aborts the whole import rather than guessing
which of two people the director meant (see plans/PLAN_PLAYER_IDENTITY.md).
"""

import csv
import io
from dataclasses import dataclass, field

from django.db import models

from tournaments.models import (
    Entrant,
    Player,
    canonical_player_number,
    next_temp_player_number,
)


@dataclass
class ImportResult:
    """Result of a bulk import operation."""
    created: list = field(default_factory=list)   # dicts with name, player_number
    matched: list = field(default_factory=list)    # player names matched to existing
    skipped: list = field(default_factory=list)    # player names already in division
    added: int = 0


def parse_csv(text):
    """Parse CSV text into a list of (number, name, rating) tuples.

    Accepts ``name``, ``name, rating`` or ``number, name, rating``. ``number`` is
    empty for the first two shapes.

    Returns (parsed_rows, errors) where errors is a list of strings.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], ["File is empty."]

    errors = []
    parsed = []
    seen = set()

    for i, row in enumerate(rows, start=1):
        row = [cell.strip() for cell in row]
        if len(row) == 1:
            number, name, rating_str = "", row[0], ""
        elif len(row) == 2:
            number, (name, rating_str) = "", row
        elif len(row) == 3:
            number, name, rating_str = row
        else:
            errors.append(f"Row {i}: expected 1, 2 or 3 columns, got {len(row)}.")
            continue

        if not name:
            errors.append(f"Row {i}: name is required.")
            continue

        number = canonical_player_number(number) if number else ""
        # Two rows for the same *person*. A repeated name with distinct numbers
        # is two people and perfectly legal; a repeated bare name is not, because
        # both rows would resolve to whoever holds it.
        key = number or name.casefold()
        if key in seen:
            errors.append(
                f"Row {i}: duplicate {'player number' if number else 'name'} "
                f"'{number or name}' in CSV."
            )
            continue
        seen.add(key)

        try:
            rating = int(rating_str) if rating_str else 0
        except ValueError:
            errors.append(f"Row {i}: invalid rating '{rating_str}'.")
            continue

        parsed.append((number, name, rating))

    return parsed, errors


def _resolve_one(number, name, row_number):
    """(player, error) for one parsed row. ``player`` is None if it must be created."""
    if number:
        player = Player.objects.filter(player_number=number).first()
        if player is None:
            return None, (
                f"Row {row_number}: no player with number '{number}'."
            )
        return player, None

    candidates = list(Player.objects.filter(name__iexact=name).order_by("player_number"))
    if len(candidates) > 1:
        listed = ", ".join(f"{p.name} (#{p.player_number})" for p in candidates)
        return None, (
            f"Row {row_number}: '{name}' matches {len(candidates)} players — "
            f"{listed}. Use the three-column form (number, name, rating) to say "
            f"which one."
        )
    return (candidates[0] if candidates else None), None


def resolve_players(parsed_rows, existing_entrant_keys):
    """Resolve parsed rows to Player objects, creating new ones as needed.

    A row carrying a player number resolves to exactly that player. A bare name
    resolves if it matches exactly one player, creates one if it matches none,
    and aborts the import if it matches several. An existing player keeps their
    current rating; the CSV rating is only used for someone being created.

    Args:
        parsed_rows: list of (number, name, rating) tuples from parse_csv.
        existing_entrant_keys: player numbers already entered in the division.

    Returns:
        (players_to_add, result, errors) where:
        - players_to_add is a list of Player objects to create Entrants for
        - result is an ImportResult with created/matched/skipped info
        - errors is a list of error strings (non-empty means abort)
    """
    errors = []
    result = ImportResult()
    players_to_add = []

    for i, (number, name, rating) in enumerate(parsed_rows, start=1):
        player, error = _resolve_one(number, name, i)
        if error:
            errors.append(error)
            continue

        if player:
            if player.player_number in existing_entrant_keys:
                result.skipped.append(player.name)
                continue
            result.matched.append(player.name)
        else:
            player = Player.objects.create(
                name=name,
                player_number=next_temp_player_number(),
                rating=rating,
                is_provisional=True,
            )
            result.created.append({"name": player.name, "player_number": player.player_number})

        players_to_add.append(player)

    return players_to_add, result, errors


def import_entrants(division, text):
    """Import entrants from CSV text into a division.

    Returns (ImportResult, errors) where errors is a list of strings.
    If errors is non-empty, no changes were made.
    """
    parsed, errors = parse_csv(text)
    if errors:
        return None, errors

    existing_entrant_keys = set(
        e.player.player_number
        for e in division.entrants.select_related("player")
    )

    players_to_add, result, errors = resolve_players(parsed, existing_entrant_keys)
    if errors:
        return None, errors

    max_number = division.entrants.aggregate(
        max_num=models.Max("number")
    )["max_num"] or 0
    for j, player in enumerate(players_to_add, start=max_number + 1):
        # enter(), not create(): the entrant pins the rating it is seeded from.
        Entrant.enter(division, player, j)

    result.added = len(players_to_add)
    return result, []
