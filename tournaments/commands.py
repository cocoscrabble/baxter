"""The command hub.

Commands are the only functions that mutate tournament state, each wrapped in
``@records_event`` so the mutation and its log entry commit together. Most
command *bodies* live in their natural domain modules (fixed_pairings.py,
generate_pairings.py, …); this module holds the registry, the CRUD commands that
have no other home (tournament/division lifecycle), and imports of the domain
modules so their decorators register on load.

Every command has the signature ``(tournament, actor, payload) -> EventResult``.
The payload is natural-key based (names, not pks) so it survives replay into a
fresh database.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.db.models import Q

from tournaments.events import EventResult, command_context, records_event
from tournaments.models import Division, DivisionSettings, Tournament

User = get_user_model()


def _resolve_editors(usernames):
    return list(User.objects.filter(username__in=usernames))


def _apply_seed(division, payload):
    """Ensure the division's settings (hence pairing_seed) exist, and reconcile
    the seed with the payload.

    A fresh division draws a random seed; record it so a replay reproduces the
    same random-strategy pairings. A replayed division carries the seed in its
    payload; set it explicitly. Returns the (possibly seed-augmented) payload.
    """
    settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
    seed = payload.get("pairing_seed")
    if seed is None:
        return {**payload, "pairing_seed": settings_obj.pairing_seed}
    if settings_obj.pairing_seed != seed:
        settings_obj.pairing_seed = seed
        settings_obj.save(update_fields=["pairing_seed"])
    return payload


# ---------------------------------------------------------------------------
# Tournament lifecycle
# ---------------------------------------------------------------------------


@records_event("tournament_created")
def create_tournament(tournament, actor, payload):
    """payload: {name, location, start_date (ISO), editors: [username],
    default_division: {name, pairing_seed}}. ``actor`` becomes the owner."""
    t = Tournament.objects.create(
        name=payload["name"],
        location=payload["location"],
        start_date=date.fromisoformat(payload["start_date"]),
        owner=actor,
    )
    editors = set(_resolve_editors(payload.get("editors", [])))
    editors.add(actor)
    t.editors.set(editors)

    # Every tournament starts with one division. Record its name + seed so a
    # replay reproduces it identically.
    default = dict(payload.get("default_division") or {})
    div = Division.objects.create(tournament=t, name=default.get("name", "Division 1"))
    default = {**default, "name": div.name, **_apply_seed(div, default)}
    out_payload = {**payload, "default_division": default}
    return EventResult(payload=out_payload, tournament=t, result=t)


@records_event("tournament_updated")
def update_tournament(tournament, actor, payload):
    """payload: {name, location, start_date (ISO), editors: [username]}."""
    tournament.name = payload["name"]
    tournament.location = payload["location"]
    tournament.start_date = date.fromisoformat(payload["start_date"])
    tournament.save()
    editors = set(_resolve_editors(payload.get("editors", [])))
    editors.add(tournament.owner)
    tournament.editors.set(editors)
    return EventResult(payload=payload, tournament=tournament, result=tournament)


def delete_tournament(tournament, actor, payload):
    """Hard delete. Can't record an event — a tournament_deleted row would
    cascade away with the tournament — so this runs in a command context (for
    the write guard) without logging."""
    with command_context():
        tournament.delete()
    return None


# ---------------------------------------------------------------------------
# Division lifecycle
# ---------------------------------------------------------------------------


@records_event("division_created")
def create_division(tournament, actor, payload):
    """payload: {name, is_test, pairing_seed}."""
    div = Division.objects.create(
        tournament=tournament,
        name=payload["name"],
        is_test=payload.get("is_test", False),
    )
    out_payload = _apply_seed(div, payload)
    return EventResult(payload=out_payload, division=div, result=div)


@records_event("division_renamed")
def rename_division(tournament, actor, payload):
    """payload: {old_name, new_name}."""
    div = Division.objects.get(tournament=tournament, name=payload["old_name"])
    div.name = payload["new_name"]
    div.save(update_fields=["name", "slug"])
    return EventResult(payload=payload, division=div, result=div)


@records_event("division_deleted")
def delete_division(tournament, actor, payload):
    """payload: {name}. Soft delete."""
    div = Division.objects.get(tournament=tournament, name=payload["name"])
    div.soft_delete()
    return EventResult(payload=payload, division=div, result=div)


@records_event("division_restored")
def restore_division(tournament, actor, payload):
    """payload: {name}. Restore a soft-deleted division."""
    div = Division.all_objects.get(tournament=tournament, name=payload["name"])
    div.restore()
    return EventResult(payload=payload, division=div, result=div)


# ---------------------------------------------------------------------------
# Publishing (thin adapters over generate_pairings; the bodies stay there)
# ---------------------------------------------------------------------------


def _division(tournament, name):
    return Division.objects.get(tournament=tournament, name=name)


@records_event("rounds_published")
def publish_all_rounds(tournament, actor, payload):
    """payload: {division}. Publishes every draft round; records which."""
    from tournaments.generate_pairings import publish_rounds

    division = _division(tournament, payload["division"])
    published = publish_rounds(division)
    out = {**payload, "rounds": published}
    return EventResult(
        payload=out, division=division, result=published, record=bool(published)
    )


@records_event("round_published")
def publish_round(tournament, actor, payload):
    """payload: {division, round}."""
    from tournaments.generate_pairings import publish_rounds

    division = _division(tournament, payload["division"])
    published = publish_rounds(division, [payload["round"]])
    return EventResult(
        payload=payload, division=division, result=published, record=bool(published)
    )


@records_event("round_unpublished")
def unpublish_round(tournament, actor, payload):
    """payload: {division, round}."""
    from tournaments.generate_pairings import unpublish_rounds

    division = _division(tournament, payload["division"])
    unpublished = unpublish_rounds(division, [payload["round"]])
    return EventResult(
        payload=payload, division=division, result=unpublished, record=bool(unpublished)
    )


# ---------------------------------------------------------------------------
# Fixed pairings (thin adapters; pk args translated to/from player names)
# ---------------------------------------------------------------------------


def _entrant(division, name):
    return division.entrants.get(player__name=name)


@records_event("fixed_pairing_added")
def add_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, name1, name2}."""
    from tournaments.fixed_pairings import add_fixed_pairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["name1"])
    e2 = _entrant(division, payload["name2"])
    ok, error = add_fixed_pairing(division, payload["round"], e1.pk, e2.pk)
    return EventResult(
        payload=payload, division=division, result=(ok, error), record=ok
    )


@records_event("fixed_pairing_removed")
def remove_fixed_pairing_cmd(tournament, actor, payload):
    """payload: {division, round, name1, name2}."""
    from tournaments.fixed_pairings import remove_fixed_pairing
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    e1 = _entrant(division, payload["name1"])
    e2 = _entrant(division, payload["name2"])
    fp = FixedPairing.objects.filter(
        division=division, round_number=payload["round"]
    ).filter(
        Q(entrant1=e1, entrant2=e2) | Q(entrant1=e2, entrant2=e1)
    ).first()
    if fp is None:
        return EventResult(payload=payload, result=(False, None), record=False)
    ok, error = remove_fixed_pairing(division, fp.pk)
    return EventResult(
        payload=payload, division=division, result=(ok, error), record=ok
    )


@records_event("fixed_pairings_removed")
def remove_fixed_pairings_cmd(tournament, actor, payload):
    """payload: {division, kept: [[round, name1, name2], ...]} — the fixed
    pairings to keep; all others are removed."""
    from tournaments.fixed_pairings import remove_fixed_pairings
    from tournaments.models import FixedPairing

    division = _division(tournament, payload["division"])
    kept = {tuple(k) for k in payload.get("kept", [])}
    keep_ids = []
    for fp in division.fixed_pairings.select_related(
        "entrant1__player", "entrant2__player"
    ):
        n1, n2 = sorted([fp.entrant1.player.name, fp.entrant2.player.name])
        if (fp.round_number, n1, n2) in kept:
            keep_ids.append(fp.pk)
    error = remove_fixed_pairings(division, keep_ids)
    return EventResult(
        payload=payload, division=division, result=error, record=error is None
    )
