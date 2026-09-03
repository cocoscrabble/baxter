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
number — is left exactly as they are.

**A roster row that looks like a local guest is held back rather than created.**
When an incoming number is new to Baxter but its name belongs to exactly one
*provisional* player, that is almost certainly the same human: a guest who has
since been given a real number centrally. Creating the row anyway would leave
two of them — one carrying the entrants and results, one carrying the number —
and the entrant would still be unexportable. So the pull reports it as needing
resolution and changes nothing, and a director confirms the rename
(``commands.change_player_number``), which is the one step that may not be
inferred silently: matching by name is the whole reason it needs a human.

Only *provisional* players are candidates. Two people who both have real numbers
and happen to share a name are two people, and the roster is right to add the
one Baxter has not seen.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from coco_ratings.identity import canonical_player_number
from django.conf import settings
from django.db import transaction

from .models import Player

SCHEMA = "coco.roster/1"


class RosterParseError(Exception):
    """The document could not be read as a roster."""


@dataclass
class PendingResolution:
    """A roster row that appears to be a local guest under their new number."""

    local_number: str      # the T- number Baxter has
    roster_number: str     # the number the registry assigned
    name: str
    row: dict              # the full roster row, applied once confirmed

    @property
    def key(self):
        """Stable identifier for a confirm form."""
        return f"{self.local_number}:{self.roster_number}"

    # A pending resolution waits on the ``RosterSync`` record between the pull
    # that found it and the click that confirms it, so it has to survive a round
    # trip through JSON — which a date does not. The conversion lives here
    # rather than in the view because ``row``'s shape is this module's business.

    def to_json(self) -> dict:
        row = dict(self.row)
        if row.get("last_played") is not None:
            row["last_played"] = row["last_played"].isoformat()
        return {
            "local_number": self.local_number,
            "roster_number": self.roster_number,
            "name": self.name,
            "row": row,
        }

    @classmethod
    def from_json(cls, data) -> "PendingResolution":
        row = dict(data["row"])
        if row.get("last_played"):
            row["last_played"] = date.fromisoformat(row["last_played"])
        return cls(
            local_number=data["local_number"],
            roster_number=data["roster_number"],
            name=data["name"],
            row=row,
        )


@dataclass
class RosterImportResult:
    added: list = field(default_factory=list)      # player numbers
    updated: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    # Rows held back for a director to confirm. Nothing was written for these.
    pending: list = field(default_factory=list)
    generated_at: str = ""

    @property
    def total(self):
        return (
            len(self.added) + len(self.updated)
            + len(self.unchanged) + len(self.pending)
        )


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
    # Provisional players by name, for the held-back check below. Only names
    # belonging to exactly one of them are candidates: two guests with the same
    # name are exactly the case a human has to disambiguate.
    provisional = {}
    for p in Player.objects.filter(is_bye=False, is_provisional=True):
        provisional.setdefault(p.name.casefold(), []).append(p)

    to_create, to_update = [], []
    for row in rows:
        player = existing.get(row["player_number"])
        if player is None:
            candidates = provisional.get(row["name"].casefold(), [])
            if len(candidates) == 1:
                result.pending.append(
                    PendingResolution(
                        local_number=candidates[0].player_number,
                        roster_number=row["player_number"],
                        name=row["name"],
                        row=row,
                    )
                )
                continue
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


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

# Long enough for a slow link, short enough that a wedged endpoint does not hold
# a request thread open indefinitely. The roster is ~40 KB.
FETCH_TIMEOUT = 30


class RosterFetchError(Exception):
    """The roster could not be retrieved. The message is shown to an admin."""


def roster_endpoint_configured() -> bool:
    """Whether a fetch is even possible. Both halves are required."""
    return bool(settings.ROSTER_API_URL and settings.ROSTER_API_TOKEN)


def fetch_roster(url=None, token=None) -> bytes:
    """GET the roster document from the central database.

    The *normal* path; the file upload is the offline one. Both hand their bytes
    to the same ``import_roster``, so nothing downstream knows which was used.

    Errors are translated into something an admin can act on, because the raw
    ones are not: a bare ``HTTPError: 401`` does not say "check the token", and
    ``URLError`` does not say "check the address".
    """
    url = url or settings.ROSTER_API_URL
    token = token or settings.ROSTER_API_TOKEN
    if not (url and token):
        raise RosterFetchError(
            "No roster endpoint is configured — set ROSTER_API_URL and "
            "ROSTER_API_TOKEN, or upload a snapshot file instead."
        )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RosterFetchError(
                f"The central database rejected the token ({url})."
            ) from None
        raise RosterFetchError(
            f"The central database returned {exc.code} {exc.reason} ({url})."
        ) from None
    except urllib.error.URLError as exc:
        raise RosterFetchError(f"Could not reach {url}: {exc.reason}.") from None
    except TimeoutError:
        raise RosterFetchError(
            f"{url} did not respond within {FETCH_TIMEOUT} seconds."
        ) from None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_number(pending, actor=None):
    """Apply one confirmed resolution: rename the guest, then pull their data.

    The rename goes through ``commands.change_player_number`` so it lands in the
    event log — the log names this player by their old number in every event up
    to now and their new one from here on, and without the event in between it
    stops being a truthful account.

    **Recorded against every tournament the player has played in**, which is
    what that command's contract promises. The rename itself happens once; the
    remaining tournaments get the same event recorded directly, because calling
    the command again would find no player on the old number and refuse.

    A player with no tournaments at all is renamed without any event. There is
    no log for it to belong to, and inventing one would be worse than the gap.
    """
    from tournaments.commands import change_player_number
    from tournaments.events import EVENT_TYPES, command_context, record_event
    from tournaments.models import Tournament

    assert "player_number_changed" in EVENT_TYPES  # guards a rename of the type

    player = Player.objects.filter(player_number=pending.local_number).first()
    if player is None:
        raise RosterParseError(
            f"{pending.name} is no longer on {pending.local_number}; "
            f"pull the roster again."
        )
    if Player.objects.filter(player_number=pending.roster_number).exists():
        raise RosterParseError(
            f"Player number {pending.roster_number} already belongs to someone "
            f"in Baxter, so {pending.name} cannot be moved onto it."
        )

    payload = {"old": pending.local_number, "new": pending.roster_number}
    tournaments = list(
        Tournament.objects.filter(
            divisions__entrants__player=player
        ).distinct().order_by("pk")
    )

    if not tournaments:
        player.player_number = pending.roster_number
        player.is_provisional = False
        player.save(update_fields=["player_number", "is_provisional"])
    else:
        change_player_number(tournaments[0], actor, payload)
        with command_context():
            for tournament in tournaments[1:]:
                record_event(
                    tournament, "player_number_changed", payload, actor=actor
                )

    # Now that they are on the registry's number, take the registry's data.
    player.refresh_from_db()
    for name in SYNCED_FIELDS:
        setattr(player, name, pending.row[name])
    player.is_provisional = False
    player.save(update_fields=[*SYNCED_FIELDS, "is_provisional"])
    return player
