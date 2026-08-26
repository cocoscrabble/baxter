"""Import a ``coco.roster/1`` document from the central player database.

This is the "before" half of the registry sync: Baxter pulls the roster, and can
then run a whole tournament with no connection to the central database at all
(``plans/PLAN_COCO_PROGRAM.md``). The document is produced two ways — an
authenticated endpoint and a downloadable snapshot file — and both are the same
bytes, so this is deliberately **one code path** that neither knows nor cares
which it was handed.

What it writes:

- ``player_number`` is the identity. Players are matched on it, never on name;
  names in the document are display data that Baxter overwrites.
- ``rating`` is the CoCo rating, which this database owns and Baxter mirrors. A
  ``null`` rating means "no rated games yet", stored as 0 — Baxter's long-
  standing convention for "no CoCo rating" (``Player.effective_rating``).
- ``deviation``, ``career_games`` and ``last_played`` are the rest of the rating
  seed, needed by the live projection.
- ``wespa_rating`` is untouched. It is not the central database's to know, and a
  pull must not clear it.

Nothing is deleted. A player the roster has never heard of — a guest on a ``T-``
number — is left exactly as they are; resolving those onto real numbers is a
separate, director-confirmed step.
"""

import json
from dataclasses import dataclass, field
from datetime import date

from coco_ratings.identity import canonical_player_number
from django.db import transaction

from .models import Player

SCHEMA = "coco.roster/1"


class RosterParseError(Exception):
    """The document could not be read as a roster."""


@dataclass
class RosterImportResult:
    added: list = field(default_factory=list)      # player numbers
    updated: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    generated_at: str = ""

    @property
    def total(self):
        return len(self.added) + len(self.updated) + len(self.unchanged)


def parse_roster(raw):
    """``(generated_at, rows)`` from a roster document (bytes, str or dict).

    Raises :class:`RosterParseError` with something a human can act on. The
    schema string is checked rather than assumed: a future ``coco.roster/2``
    should be refused loudly, not half-read.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8-sig")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RosterParseError(f"Not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise RosterParseError("Expected a roster object, not a list.")

    schema = raw.get("schema")
    if schema != SCHEMA:
        raise RosterParseError(
            f"Unsupported roster schema {schema!r} — this Baxter reads {SCHEMA!r}."
        )
    players = raw.get("players")
    if not isinstance(players, list):
        raise RosterParseError("The roster has no 'players' list.")

    rows = []
    for i, entry in enumerate(players, start=1):
        if not isinstance(entry, dict):
            raise RosterParseError(f"Player {i}: expected an object.")
        number = canonical_player_number(entry.get("player_number") or "")
        if not number:
            raise RosterParseError(f"Player {i}: no player_number.")
        name = (entry.get("name") or "").strip()
        if not name:
            raise RosterParseError(f"Player {i} (#{number}): no name.")
        rows.append(
            {
                "player_number": number,
                "name": name,
                # A null rating means no rated games yet. Baxter has always
                # spelled that 0.
                "rating": int(entry["rating"]) if entry.get("rating") is not None else 0,
                "deviation": entry.get("deviation"),
                "career_games": int(entry.get("career_games") or 0),
                "last_played": _parse_date(entry.get("last_played"), number),
            }
        )
    return raw.get("generated_at", ""), rows


def _parse_date(value, number):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise RosterParseError(
            f"Player #{number}: last_played {value!r} is not a date."
        ) from None


# What a pull owns. wespa_rating is pointedly not here.
SYNCED_FIELDS = ("name", "rating", "deviation", "career_games", "last_played")


@transaction.atomic
def import_roster(raw):
    """Upsert the roster. Returns a :class:`RosterImportResult`.

    Atomic: a roster is a coherent snapshot of one moment, and half of one is
    not a thing anyone asked for.
    """
    generated_at, rows = parse_roster(raw)
    result = RosterImportResult(generated_at=generated_at)

    existing = {p.player_number: p for p in Player.objects.filter(is_bye=False)}
    to_create, to_update = [], []
    for row in rows:
        player = existing.get(row["player_number"])
        if player is None:
            to_create.append(
                Player(
                    player_number=row["player_number"],
                    # A player the central database knows is not provisional,
                    # whatever Baxter thought before.
                    is_provisional=False,
                    **{f: row[f] for f in SYNCED_FIELDS},
                )
            )
            result.added.append(row["player_number"])
            continue
        changed = [f for f in SYNCED_FIELDS if getattr(player, f) != row[f]]
        if not changed and not player.is_provisional:
            result.unchanged.append(row["player_number"])
            continue
        for f in changed:
            setattr(player, f, row[f])
        player.is_provisional = False
        to_update.append(player)
        result.updated.append(row["player_number"])

    if to_create:
        # bulk_create bypasses save(), so the numbers are canonicalized above —
        # they came through canonical_player_number in parse_roster.
        Player.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        Player.objects.bulk_update(
            to_update, [*SYNCED_FIELDS, "is_provisional"], batch_size=500
        )
    return result
