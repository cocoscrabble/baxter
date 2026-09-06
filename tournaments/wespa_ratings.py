"""Applying the WESPA rating list: the mirror, the links, and the ratings.

``wespa_api`` knows how to fetch and read the document; this decides what it
means. Three separate things happen on a pull, and they are worth keeping apart
in your head because only the first is unconditional:

1. **The mirror is upserted.** Every row of the list lands in ``WespaPlayer``,
   whether or not Baxter has a player for it. That table is what the
   registration page searches, and searching it is the reason this integration
   exists (``plans/PLAN_WESPA.md`` decision 1).

2. **Linked players get their rating.** A ``Player.wespa_id`` is an assertion
   somebody made — a director picked the row when minting a guest, or confirmed
   it here — so it is applied without further thought, even if the two names
   have since diverged.

3. **Unlinked players are matched by name, carefully.** A name belonging to
   exactly one player *and* exactly one WESPA row links them. Anything else
   links nobody and is held back for a human, because WESPA has no idea which
   "John Smith" it means and a wrong rating is worse than a missing one.

What does *not* happen is a player being created. The list has some 9,200
players and Baxter's roster is CoCo's; a WESPA row becomes a ``Player`` only
when a director enters one (decision 2). Nor is anything deleted: a row that
drops out of the list stays in the mirror, exactly as the roster pull deletes
nothing.

``Player.name`` is never overwritten from the list either. Baxter's names are
the central database's, and WESPA's spelling of somebody is not a correction.

Refreshing ratings mutates no replayable tournament state — entrants pinned
theirs at entry (``PLAN_ENTRANTS.md`` decision 3) — so this stays an unlogged
global action, like the roster import it is modelled on. It is also what makes
an unattended weekly pull safe in the middle of an event.
"""

from dataclasses import dataclass, field

from django.db import transaction

from .models import Player, WespaPlayer
from .wespa_api import parse_wespa

# What a pull owns on a mirror row. ``wespa_id`` is the key, not a field.
MIRROR_FIELDS = ("name", "country", "rating")


@dataclass
class PendingLink:
    """A name the list and the roster both know, but not unambiguously.

    Held back rather than guessed, and carried on the ``WespaSync`` record until
    a director resolves it, for the same reason the roster's held-back rows live
    there: the pull that found it may have been a cron tick at four in the
    morning with nobody watching.

    Both sides are listed because either can be the ambiguous one — two players
    sharing a name is the common case, but nothing in the document promises its
    own names are unique.
    """

    name: str
    players: list      # [{"player_number", "name", "rating", "wespa_rating"}]
    candidates: list   # [{"wespa_id", "name", "country", "rating"}]

    @property
    def key(self):
        """Stable identifier for a confirm form."""
        return self.name.casefold()

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "players": self.players,
            "candidates": self.candidates,
        }

    @classmethod
    def from_json(cls, data) -> "PendingLink":
        return cls(
            name=data["name"],
            players=list(data["players"]),
            candidates=list(data["candidates"]),
        )


@dataclass
class WespaImportResult:
    # The mirror.
    added: list = field(default_factory=list)      # wespa ids
    updated: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    # The roster.
    rated: list = field(default_factory=list)      # player names
    linked: list = field(default_factory=list)
    # Names held back for a director. Nothing was written for these.
    pending: list = field(default_factory=list)

    @property
    def total(self):
        return len(self.added) + len(self.updated) + len(self.unchanged)


@transaction.atomic
def import_wespa(raw):
    """Upsert the mirror and apply it to the roster. Returns the result.

    Atomic: the list is a coherent snapshot of one moment, and half of one is
    not a thing anyone asked for.
    """
    rows = parse_wespa(raw)
    result = WespaImportResult()
    mirror = _upsert_mirror(rows, result)
    _apply_to_players(mirror, result)
    return result


def _upsert_mirror(rows, result):
    """Write the list into ``WespaPlayer``. Returns ``{wespa_id: WespaPlayer}``."""
    existing = {w.wespa_id: w for w in WespaPlayer.objects.all()}
    to_create, to_update = [], []
    mirror = {}

    for row in rows:
        current = existing.get(row["wespa_id"])
        if current is None:
            current = WespaPlayer(wespa_id=row["wespa_id"], **{f: row[f] for f in MIRROR_FIELDS})
            to_create.append(current)
            result.added.append(row["wespa_id"])
        else:
            changed = [f for f in MIRROR_FIELDS if getattr(current, f) != row[f]]
            if changed:
                for f in changed:
                    setattr(current, f, row[f])
                to_update.append(current)
                result.updated.append(row["wespa_id"])
            else:
                result.unchanged.append(row["wespa_id"])
        mirror[row["wespa_id"]] = current

    if to_create:
        WespaPlayer.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        WespaPlayer.objects.bulk_update(to_update, MIRROR_FIELDS, batch_size=500)
    return mirror


def _apply_to_players(mirror, result):
    """Rate the linked players, link the unambiguous ones, hold back the rest."""
    players = list(Player.objects.filter(is_bye=False))
    linked_ids = {p.wespa_id for p in players if p.wespa_id is not None}

    to_rate, to_link = [], []

    for player in players:
        if player.wespa_id is None:
            continue
        row = mirror.get(player.wespa_id)
        # A link to a row that is not in this list — the player was removed
        # upstream, or the mirror predates a change of source. The link stands
        # and the rating we already hold stands; nothing here can improve on
        # either, and dropping them would lose a human's assertion.
        if row is None or row.rating is None or player.wespa_rating == row.rating:
            continue
        player.wespa_rating = row.rating
        to_rate.append(player)
        result.rated.append(player.name)

    # Everyone else is matched by name — the only handle there is.
    ours, theirs = {}, {}
    for player in players:
        if player.wespa_id is None:
            ours.setdefault(player.name.casefold(), []).append(player)
    for row in mirror.values():
        # A row already claimed by somebody is not a candidate for anybody else.
        if row.wespa_id not in linked_ids:
            theirs.setdefault(row.name.casefold(), []).append(row)

    for key, candidates in theirs.items():
        matched = ours.get(key)
        if not matched:
            continue
        if len(matched) == 1 and len(candidates) == 1:
            player, row = matched[0], candidates[0]
            player.wespa_id = row.wespa_id
            result.linked.append(player.name)
            if row.rating is not None and player.wespa_rating != row.rating:
                player.wespa_rating = row.rating
                result.rated.append(player.name)
            to_link.append(player)
            continue
        # Ambiguous on one side or both. Nothing is written; a director picks.
        result.pending.append(
            PendingLink(
                name=candidates[0].name,
                players=[_player_json(p) for p in matched],
                candidates=[_row_json(r) for r in candidates],
            )
        )

    if to_rate:
        Player.objects.bulk_update(to_rate, ["wespa_rating"], batch_size=500)
    if to_link:
        Player.objects.bulk_update(
            to_link, ["wespa_id", "wespa_rating"], batch_size=500
        )


def _player_json(player):
    return {
        "player_number": player.player_number,
        "name": player.name,
        "rating": player.rating,
        "wespa_rating": player.wespa_rating,
    }


def _row_json(row):
    return {
        "wespa_id": row.wespa_id,
        "name": row.name,
        "country": row.country,
        "rating": row.rating,
    }


def link_player(player, wespa_player):
    """Assert that ``player`` is ``wespa_player``, and take the rating.

    The one step a human makes. Unlogged like the rest of this module: the link
    is roster data, not tournament state, and an entrant who has already been
    seeded off a typed rating keeps it.
    """
    if (
        Player.objects.filter(wespa_id=wespa_player.wespa_id)
        .exclude(pk=player.pk)
        .exists()
    ):
        raise ValueError(
            f"WESPA player {wespa_player.name} (id {wespa_player.wespa_id}) is "
            f"already linked to somebody else."
        )
    player.wespa_id = wespa_player.wespa_id
    fields = ["wespa_id"]
    if wespa_player.rating is not None:
        player.wespa_rating = wespa_player.rating
        fields.append("wespa_rating")
    player.save(update_fields=fields)
    return player


def unlink_player(player):
    """Undo a link. The rating already written stays — it was true when written,
    and a director who unlinks a wrong match will type the right one."""
    player.wespa_id = None
    player.save(update_fields=["wespa_id"])
    return player
