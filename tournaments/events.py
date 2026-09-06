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
        "division_cop_config_saved",
        "division_imported",
        "entrants_saved",
        "entrants_bulk_imported",
        "player_created",
        "player_number_changed",
        "entrant_added",
        "entrant_updated",
        "entrant_ratings_refreshed",
        "entrants_reseeded",
        "results_saved",
        "result_added",
        "result_edited",
        "result_starts_corrected",
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
        "playoff_created",
        "playoff_updated",
        "playoff_deleted",
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


def as_derived(func):
    """Decorator marking a function's writes as derived-state recomputation."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with derived_writes():
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

# The payload schema version stamped on new events. v1 payloads identified
# players by name; v2 identifies them by player number
# (plans/PLAN_PLAYER_IDENTITY.md). replay.SCHEMA_UPGRADES upgrades v1 on read.
PAYLOAD_VERSION = 2


def record_event(
    tournament,
    event_type,
    payload,
    *,
    actor=None,
    actor_session="",
    division=None,
    digest="",
    schema_version=PAYLOAD_VERSION,
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
            schema_version=schema_version,
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
    # Set False when the command validated to a no-op or rejected the input:
    # the result is still returned to the caller, but no event is logged.
    record: bool = True


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
                    if not outcome.record:
                        return outcome.result
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


# The digest's schema version. v1 identified players by name; v2 identifies
# them by player number (plans/PLAN_PLAYER_IDENTITY.md). Stored digests were
# backfilled to v2 by migration 0038.
DIGEST_VERSION = 2


def division_state(division, version: int = DIGEST_VERSION) -> dict:
    """Canonical, pk-free, timestamp-free snapshot of a division's state.

    Everything is keyed by natural identifiers (the player number, round
    numbers) and sorted, so two databases holding the same logical state — with
    different pks — produce identical output. This is what replay compares.

    ``version=1`` reproduces the *pre-identity* digest, which identified players
    by name. It exists for one caller: the migration that backfills stored
    digests, which has to prove each tournament still replays to the digest
    already recorded for it before it may rewrite an append-only log, and can
    only do that in the old vocabulary. Nothing else may pass it.

    The two versions share one body rather than being two frozen copies. A
    frozen copy could not have worked: ``standings_after_round``,
    ``final_placements`` and ``PlayoffConfig.seeds`` all moved to keys in
    phase 2, so v1 output has to be *reconstructed* from today's machinery
    rather than merely preserved. Branching in the four places the vocabularies
    differ keeps that difference visible and reviewable, where two near-identical
    90-line functions would invite exactly the silent drift the freeze was meant
    to prevent.
    """

    def ident(player):
        return player.name if version == 1 else player.player_number

    # Registration state is part of the v2 digest, so replay has to reproduce
    # it. payment_note stays out: free text that no invariant depends on, and
    # one a director may reword without changing what the tournament *is*.
    #
    # **v1 must keep the three-element tuple.** Its only caller is the backfill's
    # verification pass, which has to reproduce digests recorded before any of
    # these columns existed; extending it there would make every pre-existing
    # tournament fail to verify and be skipped. That is not hypothetical — it is
    # what happened when this extension was first written for both versions.
    def entrant_row(e):
        row = [e.number, ident(e.player), e.dropped]
        if version == 1:
            return row
        return row + [
            e.rating, e.rating_source, e.tentative, e.paid, e.playing_up
        ]

    entrants = sorted(
        entrant_row(e) for e in division.entrants.select_related("player")
    )
    # Draft rounds are transient — they're lazily regenerated (deterministically)
    # and their existence at any moment depends on when Pair Rounds was last
    # rendered, which the log doesn't capture. Excluding them keeps the digest a
    # function of *committed* state, so replay is robust to regeneration timing.
    from tournaments.models import RoundPairings

    rounds = []
    for rp in (
        division.round_pairings_set.exclude(status=RoundPairings.DRAFT).order_by("round")
    ):
        pairings = sorted(
            [
                sorted([ident(p.first.player), ident(p.second.player)]),
                p.table,
                p.table_label,
            ]
            for p in rp.pairings.select_related("first__player", "second__player")
        )
        rounds.append({"round": rp.round, "status": rp.status, "pairings": pairings})
    results = sorted(
        [
            r.round,
            ident(r.winner.player),
            ident(r.loser.player),
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
    standings = standings_after_round(
        pd, division.max_round(), include_dropped=True
    )
    state = {
        "entrants": entrants,
        "rounds": rounds,
        "results": results,
        "standings": [
            [p.name if version == 1 else p.key, p.wins, p.losses, p.ties, p.spread]
            for p in standings
        ],
    }
    # Playoff state joins the digest only when there is a playoff, so every
    # digest recorded before playoffs existed still hashes to the same value.
    from tournaments.playoff import build_bracket, final_placements, playoff_for

    playoff = playoff_for(division)
    if playoff is not None:
        bracket = build_bracket(playoff.config(), pd.result_slips)
        # Keyed on the player number: final_placements' tiebreak looks up by
        # key, and a name-keyed map would silently miss every lookup.
        numbers = {
            e.player.player_number: e.number
            for e in division.entrants.select_related("player")
        }
        # The bracket speaks in keys. Under v1 it spoke in names, so put them
        # back for the backfill's verification pass.
        if version == 1:
            names = {p.key: p.name for p in standings}

            def who(key):
                return names.get(key, key) if key else key
        else:
            def who(key):
                return key

        state["playoff"] = {
            "qualification_round": playoff.qualification_round,
            "qualifier_count": playoff.qualifier_count,
            "timing": playoff.timing,
            "stage_games": dict(sorted(playoff.stage_games.items())),
            "seeds": [who(k) for k in playoff.config().seeds],
            "series": [
                [
                    s.key, s.position, who(s.high), who(s.low), s.max_games,
                    s.start_round, s.status, who(s.winner), s.decided_by,
                    [[g.number, g.round, g.status] for g in s.games],
                ]
                for s in bracket.series
            ],
            "placements": [
                [p.place, p.name if version == 1 else p.key, p.source]
                for p in final_placements(bracket, standings, numbers)
            ],
        }
    return state


def division_digest(division, version: int = DIGEST_VERSION) -> str:
    """sha256 of a division's canonical state — stable across pk renumbering."""
    blob = json.dumps(
        division_state(division, version), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def hashed_session(request) -> str:
    """A short, non-reversible fingerprint of the caller's session, so anonymous
    actions in the audit log can be attributed to a browser without storing the
    raw session key."""
    request.session.save()  # ensure a session key exists
    key = request.session.session_key or ""
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_jsonl(tournament) -> str:
    """Serialize a tournament's log to JSONL: a header line (schema versions,
    recorded-at, git rev if available) followed by one line per event in seq
    order. The header lets a replay report which code version produced it."""
    import subprocess

    try:
        git_rev = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        git_rev = ""
    lines = [
        json.dumps(
            {
                "kind": "header",
                "tournament": tournament.name,
                # The version new events are written at. Individual events carry
                # their own, which may be older.
                "schema_version": PAYLOAD_VERSION,
                "git_rev": git_rev,
                "exported_at": _now_iso(),
            }
        )
    ]
    for event in tournament.events.order_by("seq").select_related("actor", "division"):
        lines.append(
            json.dumps(
                {
                    "seq": event.seq,
                    "created_at": event.created_at.isoformat(),
                    "actor": event.actor.username if event.actor else None,
                    "actor_session": event.actor_session,
                    "division": event.division.name if event.division else None,
                    "event_type": event.event_type,
                    "schema_version": event.schema_version,
                    "payload": event.payload,
                    "digest": event.digest,
                }
            )
        )
    return "\n".join(lines) + "\n"


def _now_iso() -> str:
    from django.utils import timezone

    return timezone.now().isoformat()


# ---------------------------------------------------------------------------
# State snapshot (for tournaments predating the log)
# ---------------------------------------------------------------------------


def build_snapshot(tournament) -> dict:
    """Full, pk-free portable state of a tournament and its divisions, for a
    one-time ``state_snapshot`` event so pre-log tournaments become replayable.

    Unlike other events this records *effect* (the derived pairings/results as
    they stand), because there is no history of inputs to re-derive from.
    """
    from tournaments.models import RoundPairings

    divisions = []
    for division in tournament.divisions.all():
        entrants = [
            {
                "number": e.number,
                # ``player`` is the number — the identity. The name and the
                # player's own ratings ride along so a replay into a fresh
                # database can recreate them; ``entrant_rating`` is the pinned
                # snapshot, which is a different thing.
                "player": e.player.player_number,
                "name": e.player.name,
                "rating": e.player.rating,
                "wespa_rating": e.player.wespa_rating,
                "entrant_rating": e.rating,
                "rating_source": e.rating_source,
                "dropped": e.dropped,
                "tentative": e.tentative,
                "paid": e.paid,
                "playing_up": e.playing_up,
                "payment_note": e.payment_note,
            }
            for e in division.entrants.select_related("player")
        ]
        try:
            settings_obj = division.settings
            seed = settings_obj.pairing_seed
            blocks = settings_obj.pairing_blocks
            round_pairings = settings_obj.round_pairings
            board_table_map = settings_obj.board_table_map
            cop_config = settings_obj.cop_config
        except Exception:
            seed, blocks, round_pairings, board_table_map, cop_config = 0, [], [], [], {}
        rounds = []
        for rp in division.round_pairings_set.exclude(
            status=RoundPairings.DRAFT
        ).order_by("round"):
            rounds.append(
                {
                    "round": rp.round,
                    "status": rp.status,
                    "pairings": [
                        {
                            "first": p.first.key,
                            "second": p.second.key,
                            "table": p.table,
                            "table_label": p.table_label,
                        }
                        for p in rp.pairings.select_related(
                            "first__player", "second__player"
                        )
                    ],
                }
            )
        results = [
            {
                "round": r.round,
                "winner": r.winner.key,
                "loser": r.loser.key,
                "winner_score": r.winner_score,
                "loser_score": r.loser_score,
                "winner_started": r.winner_started,
            }
            for r in division.result_slips.select_related("winner__player", "loser__player")
        ]
        divisions.append(
            {
                "name": division.name,
                "is_test": division.is_test,
                "pairing_seed": seed,
                "pairing_blocks": blocks,
                "round_pairings": round_pairings,
                "board_table_map": board_table_map,
                "cop_config": cop_config,
                "entrants": entrants,
                "rounds": rounds,
                "results": results,
            }
        )
    return {
        "tournament": {
            "name": tournament.name,
            "location": tournament.location,
            "start_date": tournament.start_date.isoformat(),
            "owner": tournament.owner.username,
            "editors": [
                u.username
                for u in tournament.editors.exclude(pk=tournament.owner.pk)
            ],
            "is_fake": tournament.is_fake,
        },
        "divisions": divisions,
    }


def snapshot_existing(tournament) -> "object | None":
    """Record a state_snapshot as seq 1 for a tournament that has no events yet.
    Returns the event, or None if the tournament already has a log."""
    if tournament.events.exists():
        return None
    return record_event(tournament, "state_snapshot", build_snapshot(tournament))


def describe_event(event) -> str:
    """A short human-readable description of an event, for the Activity page."""
    p = event.payload or {}
    div = event.division.name if event.division else p.get("division", "")
    t = event.event_type

    def rows(n=None):
        n = len(p.get("rows", [])) if n is None else n
        return f"{n} row{'s' if n != 1 else ''}"

    def who(*fields):
        """Names for the players a payload's ``fields`` refer to.

        Payloads identify people by number; an activity feed has to show names.
        Anything that does not resolve is shown as-is — which for a v1 payload
        is already the name, so old log lines still read correctly.
        """
        from tournaments.models import Player

        values = [p.get(f) or "" for f in fields]
        found = dict(
            Player.objects.filter(
                player_number__in=[v for v in values if v]
            ).values_list("player_number", "name")
        )
        return [found.get(v, v) for v in values]

    templates = {
        "tournament_created": lambda: f"Created tournament “{p.get('name', '')}”",
        "tournament_updated": lambda: "Updated tournament details",
        "tournament_deleted": lambda: "Deleted the tournament",
        "division_created": lambda: f"Created division “{p.get('name', '')}”",
        "division_renamed": lambda: f"Renamed “{p.get('old_name', '')}” to “{p.get('new_name', '')}”",
        "division_deleted": lambda: f"Deleted division “{p.get('name', '')}”",
        "division_restored": lambda: f"Restored division “{p.get('name', '')}”",
        "division_settings_saved": lambda: f"Saved pairing schedule for {div}",
        "division_cop_config_saved": lambda: f"Saved COP settings for {div}",
        "division_imported": lambda: f"Imported division “{p.get('name', '')}” from history",
        "entrants_saved": lambda: f"Saved entrants for {div} ({rows()})",
        "entrants_bulk_imported": lambda: f"Imported entrants for {div}",
        "results_saved": lambda: f"Saved results for {div} ({rows()})",
        "result_added": lambda: f"Entered a result in {div} round {p.get('round', '')}",
        "result_edited": lambda: f"Edited a result in {div} round {p.get('round', '')}",
        "result_starts_corrected": lambda: (
            f"Corrected {len(p.get('corrections', []))} start(s) in {div} to match "
            "the published pairings"
        ),
        "fixed_pairings_saved": lambda: f"Saved fixed pairings for {div}",
        "fixed_tables_saved": lambda: f"Saved fixed tables for {div}",
        "board_tables_saved": lambda: f"Saved the board/table map for {div}",
        # v1 payloads spelled these name1/name2; who() shows either verbatim
        # when it cannot resolve them, so both forms read the same.
        "fixed_pairing_added": lambda: "Fixed {} vs {} in {} round {}".format(
            *who("player1" if "player1" in p else "name1",
                 "player2" if "player2" in p else "name2"),
            div,
            p.get("round", ""),
        ),
        "fixed_pairing_removed": lambda: f"Removed a fixed pairing in {div} round {p.get('round', '')}",
        "fixed_pairings_removed": lambda: f"Removed fixed pairings in {div}",
        "rounds_published": lambda: f"Published rounds {p.get('rounds', '')} in {div}",
        "round_published": lambda: f"Published round {p.get('round', '')} in {div}",
        "round_unpublished": lambda: f"Unpublished round {p.get('round', '')} in {div}",
        "playoff_created": lambda: f"Created a {p.get('qualifier_count', '')}-player playoff in {div}",
        "playoff_updated": lambda: f"Reconfigured the playoff in {div}",
        "playoff_deleted": lambda: f"Removed the playoff in {div}",
        "match_simulated": lambda: f"Simulated a match in {div} round {p.get('round', '')}",
        "round_simulated": lambda: f"Simulated round {p.get('round', '')} in {div}",
        "player_created": lambda: (
            f"Created player {p.get('name', '')} (#{p.get('player_number', '')})"
        ),
        "entrant_added": lambda: "Entered {} in {}".format(*who("player"), div),
        "entrant_updated": lambda: "Updated {}'s registration in {}".format(
            *who("player"), div
        ),
        "entrant_ratings_refreshed": lambda: (
            f"Refreshed {len(p.get('entrants') or [])} entrant rating(s) in {div} "
            f"from the player table"
        ),
        "entrants_reseeded": lambda: (
            f"Renumbered {len(p.get('seeding') or [])} entrant(s) in {div} "
            f"by rating"
        ),
        "player_number_changed": lambda: (
            f"Changed a player number from {p.get('old', '')} to {p.get('new', '')}"
        ),
        "state_snapshot": lambda: f"Recorded a state snapshot for {div}",
    }
    render = templates.get(t)
    return render() if render else t.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# Development write guard
# ---------------------------------------------------------------------------

# The guard is opt-in: it only raises while a ``strict_write_guard()`` context is
# active. That keeps the ordinary test suite (which builds fixtures with direct
# ORM writes) working, while letting the fuzzer and replay tests assert that no
# mutation escapes a command. A write inside a command or derived-recompute
# context is always allowed.
_guard_strict: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "guard_strict", default=False
)


class strict_write_guard:
    """Within this context, any ORM write to a guarded model outside a command
    (or derived_writes) raises — for the fuzzer and replay harness to catch a
    mutation that skipped the event log."""

    def __enter__(self):
        self._token = _guard_strict.set(True)
        return self

    def __exit__(self, *exc):
        _guard_strict.reset(self._token)
        return False


def _write_guard(sender, **kwargs):
    if in_command_context() or not _guard_strict.get():
        return
    raise RuntimeError(
        f"event-log guard: {sender.__name__} written outside a command"
    )


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
