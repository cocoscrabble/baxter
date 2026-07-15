"""Append-only tournament event log: recording machinery, the command catalog,
the ``@records_event`` decorator, the state digest, and the development-mode
write guard.

An event records a command's validated *inputs*; derived state (generated
pairings, materialized byes, round statuses, standings) is recomputed on replay,
which is what makes replay a test. See PLAN_EVENT_LOG.md.
"""

import contextvars
import dataclasses
import functools
import hashlib
import json
import logging

from django.db import models, transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event catalog
# ---------------------------------------------------------------------------

# Every state-changing command's event type. The completeness guard (Phase 2)
# asserts that every mutating view maps to one of these; adding a mutation path
# without an event type fails that test.
EVENT_TYPES = frozenset(
    {
        "tournament_created",
        "tournament_updated",
        "tournament_deleted",
        "division_created",
        "division_renamed",
        "division_deleted",
        "division_restored",
        "division_settings_saved",
        "entrants_saved",
        "entrants_bulk_imported",
        "player_created",
        "results_saved",
        "result_added",
        "result_edited",
        "fixed_pairings_saved",
        "fixed_tables_saved",
        "board_tables_saved",
        "fixed_pairing_added",
        "fixed_pairing_removed",
        "fixed_pairings_removed",
        "rounds_published",
        "round_published",
        "round_unpublished",
        "pairing_seed_rerolled",
        "match_simulated",
        "round_simulated",
        # A synthesized full-state event for tournaments predating the log.
        "state_snapshot",
    }
)


# ---------------------------------------------------------------------------
# Command context
# ---------------------------------------------------------------------------

# Marks that execution is inside a command (a recorded mutation) or a derived
# recomputation (regenerate_pairings / materialize_byes — not logged, but a
# legitimate ORM write). The development write guard consults these.
_in_command: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_command", default=False
)
_in_derived: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_derived", default=False
)


def in_command_context() -> bool:
    return _in_command.get() or _in_derived.get()


class derived_writes:
    """Context manager marking derived-state recomputation (regenerate_pairings,
    materialize_byes). Writes inside are legitimate but unlogged, so the guard
    permits them without a command."""

    def __enter__(self):
        self._token = _in_derived.set(True)
        return self

    def __exit__(self, *exc):
        _in_derived.reset(self._token)
        return False


class command_context:
    """Mark a mutation as command-driven for the write guard *without* recording
    an event. For the rare mutation that can't carry a log entry — tournament
    deletion cascades its own log away, so there is nothing to record against."""

    def __enter__(self):
        self._token = _in_command.set(True)
        return self

    def __exit__(self, *exc):
        _in_command.reset(self._token)
        return False


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_event(
    tournament,
    event_type,
    payload,
    *,
    actor=None,
    actor_session="",
    division=None,
    digest="",
):
    """Append one event to ``tournament``'s log, allocating the next ``seq``.

    Must run inside the command's transaction so the event and the state it
    describes commit together (a failed command leaves no event). The next seq
    is allocated under a row lock on the Tournament so concurrent commands can't
    collide (SQLite serializes writes anyway; Postgres needs the lock).
    """
    from tournaments.models import Tournament, TournamentEvent

    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}")

    with transaction.atomic():
        # Lock the tournament row to serialize seq allocation across commands.
        Tournament.objects.select_for_update().filter(pk=tournament.pk).exists()
        last = (
            TournamentEvent.objects.filter(tournament=tournament).aggregate(
                m=models.Max("seq")
            )["m"]
            or 0
        )
        return TournamentEvent.objects.create(
            tournament=tournament,
            seq=last + 1,
            event_type=event_type,
            payload=payload,
            actor=actor,
            actor_session=actor_session,
            division=division,
            digest=digest,
        )


@dataclasses.dataclass
class EventResult:
    """What a command returns: the payload to log plus metadata.

    ``payload`` is normally the command's validated input dict, verbatim (so a
    replay drives the same command with the same payload). A command may augment
    it — the one intended case is simulated results recording their generated
    scores. ``division`` is the affected division (digest source + convenience
    FK); ``tournament`` overrides it when there is no division (or the tournament
    is being created). ``result`` is handed back to the view caller.
    """

    payload: dict
    division: object = None
    tournament: object = None
    result: object = None


# event_type -> command callable, populated by @records_event. Used by replay.
COMMAND_REGISTRY: dict[str, object] = {}


def records_event(event_type):
    """Mark a function as a command: it runs in a transaction (as an "in command"
    context), and the event it returns is appended in that same transaction.

    The command's signature is ``func(tournament, actor, payload) -> EventResult``
    (``tournament`` may be ``None`` when the command creates it). Registration
    (``event_type -> callable``) lets the replay harness dispatch by type.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(tournament, actor, payload, *, actor_session=""):
            token = _in_command.set(True)
            try:
                with transaction.atomic():
                    outcome = func(tournament, actor, payload)
                    division = outcome.division
                    tourn = (
                        outcome.tournament
                        or (division.tournament if division is not None else None)
                        or tournament
                    )
                    digest = division_digest(division) if division is not None else ""
                    record_event(
                        tourn,
                        event_type,
                        outcome.payload,
                        actor=actor,
                        actor_session=actor_session,
                        division=division,
                        digest=digest,
                    )
                    return outcome.result
            finally:
                _in_command.reset(token)

        wrapper._event_type = event_type
        COMMAND_REGISTRY[event_type] = wrapper
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# State digest
# ---------------------------------------------------------------------------


def division_state(division) -> dict:
    """Canonical, pk-free, timestamp-free snapshot of a division's state.

    Everything is keyed by natural identifiers (player names, round numbers) and
    sorted, so two databases holding the same logical state — with different
    pks — produce identical output. This is what replay compares.
    """
    entrants = sorted(
        [e.number, e.player.name, e.dropped]
        for e in division.entrants.select_related("player")
    )
    rounds = []
    for rp in division.round_pairings_set.order_by("round"):
        pairings = sorted(
            [
                sorted([p.first.player.name, p.second.player.name]),
                p.table,
                p.table_label,
            ]
            for p in rp.pairings.select_related("first__player", "second__player")
        )
        rounds.append({"round": rp.round, "status": rp.status, "pairings": pairings})
    results = sorted(
        [
            r.round,
            r.winner.player.name,
            r.loser.player.name,
            r.winner_score,
            r.loser_score,
            r.winner_started,
        ]
        for r in division.result_slips.select_related("winner__player", "loser__player")
    )
    # Standings after the last played round (dropped players kept, as the
    # display shows them). Imported lazily to avoid a model-import cycle.
    from tournaments.pairing.base import PairingData, standings_after_round

    pd = PairingData.for_division(division)
    standings = [
        [p.name, p.wins, p.losses, p.ties, p.spread]
        for p in standings_after_round(pd, division.max_round(), include_dropped=True)
    ]
    return {
        "entrants": entrants,
        "rounds": rounds,
        "results": results,
        "standings": standings,
    }


def division_digest(division) -> str:
    """sha256 of a division's canonical state — stable across pk renumbering."""
    blob = json.dumps(division_state(division), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Development write guard
# ---------------------------------------------------------------------------

# Once Phase 2 has routed every mutation through a command, this flips to True
# and any ORM write to a guarded model outside a command/derived context raises
# in dev and tests. Until then it only debug-logs, so nothing breaks mid-build.
GUARD_STRICT = False


def _write_guard(sender, **kwargs):
    if in_command_context():
        return
    message = f"event-log guard: {sender.__name__} written outside a command"
    if GUARD_STRICT:
        raise RuntimeError(message)
    logger.debug(message)


def connect_write_guard():
    """Install the guard on tournament-scoped *intent* models (dev/tests only).

    Derived models (Pairing, RoundPairings) are excluded — they're rewritten by
    regenerate_pairings, which marks itself via ``derived_writes``. Called from
    the app's ``ready()``.
    """
    import sys

    from django.conf import settings
    from django.db.models.signals import pre_delete, pre_save

    if not (settings.DEBUG or "test" in sys.argv):
        return

    from tournaments.models import (
        Division,
        DivisionSettings,
        Entrant,
        FixedPairing,
        FixedTable,
        ResultSlip,
        Tournament,
    )

    for model in (
        Tournament,
        Division,
        DivisionSettings,
        Entrant,
        ResultSlip,
        FixedPairing,
        FixedTable,
    ):
        pre_save.connect(
            _write_guard, sender=model, dispatch_uid=f"eventguard_save_{model.__name__}"
        )
        pre_delete.connect(
            _write_guard,
            sender=model,
            dispatch_uid=f"eventguard_delete_{model.__name__}",
        )
