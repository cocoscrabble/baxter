"""Export and import the global Player roster as a portable JSON list.

Used to push a local player database to a remote Baxter instance: run
``manage.py export_players`` locally to produce a file, then upload it through
the admin-only import endpoint on the remote.

The exchange format is a JSON list of ``{player_number, name, rating}``.
``player_number`` is the stable identity, so importing upserts on it: existing
players are matched and updated in place, new ones are inserted, and nothing is
deleted. Primary keys are never touched, so the ``Entrant`` rows that reference
players by PK on the remote stay intact.
"""

import json

from django.db import transaction

from .models import Player


def export_players():
    """Return every player as a plain dict, ordered by player_number.

    The synthetic bye player is excluded — it is internal and never syncs to the
    registry."""
    return [
        {"player_number": p.player_number, "name": p.name, "rating": p.rating}
        for p in Player.objects.filter(is_bye=False).order_by("player_number")
    ]


def import_players(raw):
    """Upsert players from ``raw`` (parsed JSON, a JSON string, or bytes).

    Matches existing players on ``player_number``: updates name/rating when they
    differ, inserts the rest, never deletes. Returns ``(result, errors)``. On any
    validation error nothing is written, ``result`` is ``None`` and ``errors`` is
    a non-empty list; otherwise ``result`` is a dict of counts and ``errors`` is
    empty.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, ["File is not valid JSON."]

    if not isinstance(raw, list):
        return None, ["Expected a JSON list of players."]

    # Validate and normalise, de-duplicating within the upload (last wins).
    cleaned = {}
    errors = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            errors.append(f"Row {i + 1}: expected an object.")
            continue
        number = str(row.get("player_number", "")).strip()
        name = str(row.get("name", "")).strip()
        if not number:
            errors.append(f"Row {i + 1}: player_number is required.")
            continue
        if not name:
            errors.append(f"Row {i + 1}: name is required.")
            continue
        try:
            rating = int(row.get("rating", 0) or 0)
        except (ValueError, TypeError):
            errors.append(f"Row {i + 1}: rating must be a number.")
            continue
        cleaned[number] = {"name": name, "rating": rating}

    if errors:
        return None, errors

    existing = {p.player_number: p for p in Player.objects.all()}
    to_create = []
    updated = 0
    with transaction.atomic():
        for number, fields in cleaned.items():
            player = existing.get(number)
            if player is None:
                to_create.append(Player(
                    player_number=number,
                    name=fields["name"],
                    rating=fields["rating"],
                ))
            elif player.name != fields["name"] or player.rating != fields["rating"]:
                player.name = fields["name"]
                player.rating = fields["rating"]
                player.save(update_fields=["name", "rating"])
                updated += 1
        Player.objects.bulk_create(to_create)

    return {
        "total": len(cleaned),
        "added": len(to_create),
        "updated": updated,
        "unchanged": len(cleaned) - len(to_create) - updated,
    }, []
