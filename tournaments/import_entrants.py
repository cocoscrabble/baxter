"""Bulk import of entrants from CSV data.

Parses CSV rows, resolves or creates Players, and adds Entrants to a Division.
"""

import csv
import io
from dataclasses import dataclass, field

from django.db import models

from tournaments.models import Entrant, Player, next_temp_player_number


@dataclass
class ImportResult:
    """Result of a bulk import operation."""
    created: list = field(default_factory=list)   # dicts with name, player_number
    matched: list = field(default_factory=list)    # player names matched to existing
    skipped: list = field(default_factory=list)    # player names already in division
    added: int = 0


def parse_csv(text):
    """Parse CSV text into a list of (name, rating) tuples.

    Accepts 1-column (name) or 2-column (name, rating) format.
    Returns (parsed_rows, errors) where errors is a list of strings.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], ["File is empty."]

    errors = []
    parsed = []
    seen_names = set()

    for i, row in enumerate(rows, start=1):
        row = [cell.strip() for cell in row]
        if len(row) == 1:
            name = row[0]
            rating_str = ""
        elif len(row) == 2:
            name, rating_str = row
        else:
            errors.append(f"Row {i}: expected 1 or 2 columns, got {len(row)}.")
            continue

        if not name:
            errors.append(f"Row {i}: name is required.")
            continue

        name_lower = name.lower()
        if name_lower in seen_names:
            errors.append(f"Row {i}: duplicate name '{name}' in CSV.")
            continue
        seen_names.add(name_lower)

        try:
            rating = int(rating_str) if rating_str else 0
        except ValueError:
            errors.append(f"Row {i}: invalid rating '{rating_str}'.")
            continue

        parsed.append((name, rating))

    return parsed, errors


def resolve_players(parsed_rows, existing_entrant_names):
    """Resolve parsed rows to Player objects, creating new ones as needed.

    Looks up players by name (case-insensitive). If found, uses the existing
    player (ignoring the CSV rating). If not found, creates a new player with
    the CSV rating.

    Args:
        parsed_rows: list of (name, rating) tuples from parse_csv.
        existing_entrant_names: set of lowercased player names already in the division.

    Returns:
        (players_to_add, result, errors) where:
        - players_to_add is a list of Player objects to create Entrants for
        - result is an ImportResult with created/matched/skipped info
        - errors is a list of error strings (non-empty means abort)
    """
    errors = []
    result = ImportResult()
    players_to_add = []

    for name, rating in parsed_rows:
        player = Player.objects.filter(name__iexact=name).first()

        if player:
            if player.name.lower() in existing_entrant_names:
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

    existing_entrant_names = set(
        e.player.name.lower()
        for e in division.entrants.select_related("player")
    )

    players_to_add, result, errors = resolve_players(parsed, existing_entrant_names)
    if errors:
        return None, errors

    max_number = division.entrants.aggregate(
        max_num=models.Max("number")
    )["max_num"] or 0
    for j, player in enumerate(players_to_add, start=max_number + 1):
        Entrant.objects.create(division=division, player=player, number=j)

    result.added = len(players_to_add)
    return result, []
