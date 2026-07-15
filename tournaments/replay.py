"""Replay a tournament's event log into the current database.

Each event is applied through the same command functions that recorded it
(registered in ``events.COMMAND_REGISTRY``), or, for the grid-save events, by
re-driving the grid's persist from the portable payload. Derived state
(generated pairings, materialized byes) is *not* in the log — it is recomputed
here at the points where the live app would have, which is what makes replay a
faithful reconstruction and, with ``--verify``, a test.

See PLAN_EVENT_LOG.md.
"""

import json

from django.contrib.auth import get_user_model

from tournaments.events import COMMAND_REGISTRY, command_context, division_digest
from tournaments.grids import (
    BoardTableMapGrid,
    EntrantsGrid,
    FixedPairingsGrid,
    FixedTablesGrid,
    ResultsGrid,
)

User = get_user_model()


class ReplayError(Exception):
    pass


# Grid-save events aren't @records_event commands; replay re-drives the grid.
GRID_BY_EVENT = {
    "entrants_saved": EntrantsGrid(),
    "results_saved": ResultsGrid(),
    "fixed_pairings_saved": FixedPairingsGrid(),
    "fixed_tables_saved": FixedTablesGrid(),
    "board_tables_saved": BoardTableMapGrid(),
}

# Events that consume draft pairings; regenerate the division first so the draft
# they act on exists (reproducing the lazy Pair-Rounds render the director did).
NEEDS_REGEN = {"round_published", "rounds_published"}

# Schema-version upgrade hooks: event_type -> function(payload, from_version) ->
# payload. Empty to start; add small upgraders when an event schema changes.
SCHEMA_UPGRADES: dict = {}


class ReplayContext:
    def __init__(self):
        self.tournament = None
        self._actors = {}

    def actor(self, username):
        if not username:
            return None
        if username not in self._actors:
            self._actors[username], _ = User.objects.get_or_create(username=username)
        return self._actors[username]


def parse_jsonl(text):
    """Parse exported JSONL into (header, [event dicts]) in seq order."""
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not lines or lines[0].get("kind") != "header":
        raise ReplayError("missing header line")
    return lines[0], lines[1:]


def _upgrade(event):
    upgrader = SCHEMA_UPGRADES.get(event["event_type"])
    if upgrader is not None:
        event = {**event, "payload": upgrader(event["payload"], event.get("schema_version", 1))}
    return event


def _replay_grid(division, event_type, payload):
    grid = GRID_BY_EVENT[event_type]
    rows = grid.from_portable(payload["rows"], division)
    validated, errors = grid.validate(rows, division)
    if errors:
        raise ReplayError(f"{event_type}: {errors}")
    prepared, errors = grid.prepare(division, validated)
    if errors:
        raise ReplayError(f"{event_type}: {errors}")
    with command_context():
        grid.persist(division, prepared)
        grid.after_save(division)


def apply_event(ctx, event):
    from tournaments.commands import create_tournament, delete_tournament
    from tournaments.generate_pairings import regenerate_pairings

    event = _upgrade(event)
    event_type = event["event_type"]
    payload = event["payload"]
    actor = ctx.actor(event.get("actor"))

    if event_type == "tournament_created":
        ctx.tournament = create_tournament(None, actor, payload)
        return
    if ctx.tournament is None:
        raise ReplayError(f"event {event_type} before tournament_created")
    if event_type == "tournament_deleted":
        delete_tournament(ctx.tournament, actor, payload)
        ctx.tournament = None
        return

    division = None
    if payload.get("division"):
        division = ctx.tournament.divisions.filter(name=payload["division"]).first()

    if event_type in NEEDS_REGEN and division is not None:
        regenerate_pairings(division)

    if event_type in GRID_BY_EVENT:
        if division is None:
            raise ReplayError(f"{event_type}: division {payload.get('division')!r} not found")
        _replay_grid(division, event_type, payload)
        return

    command = COMMAND_REGISTRY.get(event_type)
    if command is None:
        raise ReplayError(f"no replay handler for event type {event_type!r}")
    command(ctx.tournament, actor, payload, actor_session=event.get("actor_session", ""))


def replay(events, *, verify=False, upto=None):
    """Apply ``events`` (dicts in seq order) into the current DB.

    ``upto`` stops after that seq. ``verify`` compares each event's recorded
    digest against the replayed division's digest and raises on the first
    mismatch. Returns the ReplayContext (with the reconstructed tournament).
    """
    ctx = ReplayContext()
    for event in events:
        if upto is not None and event.get("seq", 0) > upto:
            break
        apply_event(ctx, event)
        if verify and event.get("digest") and event.get("division") and ctx.tournament:
            division = ctx.tournament.divisions.filter(name=event["division"]).first()
            if division is None:
                continue
            actual = division_digest(division)
            if actual != event["digest"]:
                raise ReplayError(
                    f"digest mismatch at seq {event.get('seq')} "
                    f"({event['event_type']}): recorded {event['digest']} "
                    f"!= replayed {actual}"
                )
    return ctx


def events_from_tournament(tournament):
    """The live event rows of ``tournament`` as replay dicts (for DB replay)."""
    events = []
    for e in tournament.events.order_by("seq").select_related("actor", "division"):
        events.append(
            {
                "seq": e.seq,
                "actor": e.actor.username if e.actor else None,
                "actor_session": e.actor_session,
                "division": e.division.name if e.division else e.payload.get("division"),
                "event_type": e.event_type,
                "schema_version": e.schema_version,
                "payload": e.payload,
                "digest": e.digest,
            }
        )
    return events
