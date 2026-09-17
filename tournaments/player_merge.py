"""Merging a guest player into the CoCo player they turned out to be.

The roster pull resolves a guest by renumbering them, but only when their name
belongs to exactly one guest (``roster_import``). A name shared by two guests is
a guess it will not make, so it creates the real player as a third row — and
from then on a renumbering is impossible, because the number is taken. This is
the way out: every guest whose name matches a CoCo player's is offered for
merging into that player (``commands.merge_player``).

Matching is by name, so nothing here merges on its own; an admin confirms each.
"""

from dataclasses import dataclass, field

from django.db import transaction

from .models import Player, Tournament


@dataclass
class MergeCandidate:
    """One guest, with the CoCo players who share their name."""

    guest: Player
    targets: list[Player]
    tournaments: list[Tournament] = field(default_factory=list)


def merge_candidates():
    """Every guest whose name matches at least one CoCo player's, by name.

    A name shared by two CoCo players offers both: which one the guest was is
    exactly what the admin is there to say.
    """
    coco = {}
    for player in Player.objects.filter(is_bye=False, is_provisional=False):
        coco.setdefault(player.name.casefold(), []).append(player)

    candidates = []
    for guest in Player.objects.filter(is_bye=False, is_provisional=True).order_by(
        "name", "player_number"
    ):
        targets = coco.get(guest.name.casefold())
        if targets:
            candidates.append(
                MergeCandidate(
                    guest=guest,
                    targets=sorted(targets, key=lambda p: p.player_number),
                    tournaments=_tournaments_of(guest),
                )
            )
    return candidates


def _tournaments_of(player):
    return list(
        Tournament.objects.filter(divisions__entrants__player=player)
        .distinct()
        .order_by("pk")
    )


@transaction.atomic
def merge_guest(guest, into, actor=None):
    """Merge ``guest`` into ``into``, logged in every tournament the guest played.

    The same shape as ``roster_import.resolve_number``: the command runs once, in
    the first tournament, and the rest get the same event recorded directly —
    running the command again would find the guest already gone. Either log then
    replays on its own. A guest with no tournaments is merged with no event,
    since there is no log for it to belong to. Atomic, so a refusal part-way
    leaves no tournament logging a merge that did not happen.
    """
    from .commands import apply_player_merge, merge_player
    from .events import command_context, record_event

    if not guest.is_provisional:
        raise ValueError(f"{guest.name} ({guest.player_number}) is not a guest.")
    if into.is_provisional:
        raise ValueError(f"{into.name} ({into.player_number}) is a guest too.")

    payload = {"guest": guest.player_number, "into": into.player_number}
    tournaments = _tournaments_of(guest)
    if not tournaments:
        with command_context():
            return apply_player_merge(guest.player_number, into.player_number)

    merged = merge_player(tournaments[0], actor, payload)
    with command_context():
        for tournament in tournaments[1:]:
            record_event(tournament, "player_merged", payload, actor=actor)
    return merged
