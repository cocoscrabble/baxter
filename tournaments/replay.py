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

# ---------------------------------------------------------------------------
# Schema upgrades
# ---------------------------------------------------------------------------

# v1 payloads identified players by name; v2 identifies them by player number
# (plans/PLAN_PLAYER_IDENTITY.md). Upgrading is *safe* only because v1 Baxter
# enforced globally unique player names, so a v1 name resolves to exactly one
# player — an assumption that stops holding the moment phase 4 lands, which is
# why the upgrade happens on read and the log is backfilled rather than
# reinterpreted every time.
#
# Two v1 shapes need no upgrader at all, because their player references are
# internal to the payload rather than pointers into the database: a
# ``state_snapshot`` and an ``entrants_saved`` row carry whatever token they
# carry, and the readers below only require those tokens to match each other.


def _number_for(name):
    """The player number of the (unique, under v1) player called ``name``.

    Resolved against the database as it stands at this point in the replay,
    which is where the earlier events have already put the roster. An unknown
    name is left alone: the command that consumes it will fail loudly rather
    than silently act on the wrong person.
    """
    from tournaments.models import Player

    player = Player.objects.filter(name__iexact=name).first()
    return player.player_number if player else name


def _rename(payload, mapping):
    """Rename payload keys, resolving each renamed value from a name to a number."""
    out = dict(payload)
    for old, new in mapping.items():
        if old in out:
            out[new] = _number_for(out.pop(old))
    return out


_RESULT_FIELDS = {
    "first_name": "first_player",
    "second_name": "second_player",
    "winner_name": "winner_player",
    "loser_name": "loser_player",
}
_PAIR_FIELDS = {"name1": "player1", "name2": "player2"}


def _upgrade_pair(payload, from_version):
    return payload if from_version >= 2 else _rename(payload, _PAIR_FIELDS)


def _upgrade_result(payload, from_version):
    return payload if from_version >= 2 else _rename(payload, _RESULT_FIELDS)


def _upgrade_kept_pairings(payload, from_version):
    """``kept`` holds [round, name1, name2] triples, sorted by name.

    Re-sorting after the rename matters: the consumer compares against a tuple
    sorted by *number*, and name order and number order are unrelated.
    """
    if from_version >= 2:
        return payload
    kept = [
        [round_number, *sorted([_number_for(a), _number_for(b)])]
        for round_number, a, b in payload.get("kept", [])
    ]
    return {**payload, "kept": kept}


def _upgrade_simulated_match(payload, from_version):
    if from_version >= 2:
        return payload
    out = _rename(payload, _RESULT_FIELDS)
    if "result" in out:
        out["result"] = _rename(out["result"], _RESULT_FIELDS)
    return out


def _upgrade_simulated_round(payload, from_version):
    if from_version >= 2:
        return payload
    return {
        **payload,
        "results": [_rename(r, _RESULT_FIELDS) for r in payload.get("results", [])],
    }


def _upgrade_rows(*fields):
    """An upgrader for a grid-save payload whose ``rows`` name players."""

    def upgrade(payload, from_version):
        if from_version >= 2:
            return payload
        rows = [
            {**r, **{f: _number_for(r[f]) for f in fields if r.get(f) is not None}}
            for r in payload.get("rows", [])
        ]
        return {**payload, "rows": rows}

    return upgrade


# Schema-version upgrade hooks: event_type -> function(payload, from_version) ->
# payload.
SCHEMA_UPGRADES: dict = {
    "fixed_pairing_added": _upgrade_pair,
    "fixed_pairing_removed": _upgrade_pair,
    "fixed_pairings_removed": _upgrade_kept_pairings,
    "result_added": _upgrade_result,
    "result_edited": _upgrade_result,
    "match_simulated": _upgrade_simulated_match,
    "round_simulated": _upgrade_simulated_round,
    "results_saved": _upgrade_rows("winner", "loser"),
    "fixed_pairings_saved": _upgrade_rows("entrant1", "entrant2"),
    "fixed_tables_saved": _upgrade_rows("entrant"),
}


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


def _replay_snapshot(ctx, payload):
    """Rebuild a whole tournament from a state_snapshot (recorded effect, since a
    pre-log tournament has no input history to re-derive from)."""
    from datetime import date

    from tournaments.grids import resolve_player
    from tournaments.models import (
        BYE_PLAYER_NUMBER,
        Division,
        DivisionSettings,
        Entrant,
        Pairing,
        ResultSlip,
        RoundPairings,
        Tournament,
    )

    t = payload["tournament"]
    owner = ctx.actor(t["owner"])
    tournament = Tournament.objects.create(
        name=t["name"],
        location=t["location"],
        start_date=date.fromisoformat(t["start_date"]),
        owner=owner,
        is_fake=t.get("is_fake", False),
    )
    tournament.editors.set(
        [u for u in [owner] + [ctx.actor(n) for n in t.get("editors", [])] if u]
    )
    ctx.tournament = tournament

    with command_context():
        for d in payload["divisions"]:
            division = Division.objects.create(
                tournament=tournament, name=d["name"], is_test=d["is_test"]
            )
            settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
            settings_obj.pairing_seed = d.get("pairing_seed", 0)
            settings_obj.pairing_blocks = d.get("pairing_blocks", [])
            settings_obj.round_pairings = d.get("round_pairings", [])
            settings_obj.board_table_map = d.get("board_table_map", [])
            settings_obj.cop_config = d.get("cop_config", {})
            settings_obj.save()

            # A v1 snapshot's "player" is a name and there is no "name" key;
            # a v2 snapshot's is a number. Either way the payload's references
            # to it — pairings, results — use the same token, so the map below
            # is keyed on whatever that token is and nothing else has to care.
            ent_by_key = {}
            for e in d["entrants"]:
                v2 = "name" in e
                player = resolve_player(
                    e["player"] if v2 else None,
                    e.get("name", e["player"]),
                    e.get("rating", 0),
                )
                ent_by_key[e["player"]] = Entrant.enter(
                    division, player, e["number"],
                    rating=e.get("rating"),
                    dropped=e.get("dropped", False),
                )

            def resolve(key):
                if key.casefold() == BYE_PLAYER_NUMBER.casefold():
                    return division.bye_entrant()
                return ent_by_key.get(key) or Entrant.enter(
                    division, resolve_player(key), 1000 + len(ent_by_key),
                )

            pairing_by_key = {}
            for rd in d["rounds"]:
                rp = RoundPairings.objects.create(
                    division=division, round=rd["round"], status=rd["status"]
                )
                for p in rd["pairings"]:
                    first, second = resolve(p["first"]), resolve(p["second"])
                    pairing = Pairing.objects.create(
                        division=division, round=rd["round"], round_pairings=rp,
                        first=first, second=second,
                        table=p.get("table", 0), table_label=p.get("table_label", ""),
                    )
                    key = (rd["round"], frozenset({p["first"], p["second"]}))
                    pairing_by_key[key] = pairing

            for r in d["results"]:
                key = (r["round"], frozenset({r["winner"], r["loser"]}))
                ResultSlip.objects.create(
                    division=division, round=r["round"],
                    pairing=pairing_by_key.get(key),
                    winner=resolve(r["winner"]), winner_score=r["winner_score"],
                    loser=resolve(r["loser"]), loser_score=r["loser_score"],
                    winner_started=r["winner_started"],
                )


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
    if event_type == "state_snapshot":
        _replay_snapshot(ctx, payload)
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
