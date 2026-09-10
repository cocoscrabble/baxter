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
import re

from django.db import models, transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event catalog
# ---------------------------------------------------------------------------

# Every state-changing command's event type. The completeness guard asserts
# that every mutating view maps to one of these; adding a mutation path
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
        "division_absence_settings_saved",
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
        "entrant_withdrawn",
        "entrant_rejoined",
        "game_forfeited",
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
# players by name; v2 identifies them by player number.
# replay.SCHEMA_UPGRADES upgrades v1 on read.
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
# them by player number. Stored digests were backfilled to v2 by migration 0038.
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


def describe_event(event, names=None) -> str:
    """A short human-readable description of an event, for the Activity page.

    ``names`` is the page's already-resolved {identifier: name} map; without it
    the lookup below runs per event, which is one query per row.
    """
    p = event.payload or {}
    div = event.division.name if event.division else p.get("division", "")
    t = event.event_type

    def rows(n=None):
        n = len(p.get("rows", [])) if n is None else n
        return f"{n} row{'s' if n != 1 else ''}"

    def changes():
        """What a grid save did: "2 added, 1 changed" for a delta payload, or
        the row count for a whole-collection one (every payload written before
        grid saves were logged as deltas, and the grids still logged that way).
        """
        if "rows" in p:
            return rows()
        counted = [
            f"{len(p[k])} {k}" for k in ("added", "changed", "removed") if p.get(k)
        ]
        return ", ".join(counted) or "no changes"

    def resolved():
        """" (N round(s) repaired)" — what a withdrawal did to printed rounds."""
        n = len(p.get("resolved", []))
        return f" ({n} printed round{'s' if n != 1 else ''} repaired)" if n else ""

    def who(*fields):
        """Names for the players a payload's ``fields`` refer to.

        Payloads identify people by number; an activity feed has to show names.
        Anything that does not resolve is shown as-is — which for a v1 payload
        is already the name, so old log lines still read correctly.
        """
        values = [p.get(f) or "" for f in fields]
        found = names
        if found is None:
            from tournaments.models import Player

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
        "division_absence_settings_saved": lambda: (
            f"Set {div} to score absences as “{p.get('withdrawal', '')}” "
            f"at {p.get('bye_spread', '')} points"
        ),
        "division_imported": lambda: f"Imported division “{p.get('name', '')}” from history",
        "entrants_saved": lambda: f"Saved entrants for {div} ({changes()})",
        "entrants_bulk_imported": lambda: f"Imported entrants for {div}",
        "results_saved": lambda: f"Saved results for {div} ({changes()})",
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
        "entrant_withdrawn": lambda: "{} withdrew from {}{}".format(
            *who("player"), div, resolved()
        ),
        "entrant_rejoined": lambda: "{} rejoined {}".format(*who("player"), div),
        "game_forfeited": lambda: "{} forfeited round {} in {}".format(
            *who("player"), p.get("round", ""), div
        ),
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
        # A snapshot is the whole tournament, so it names no division — and
        # "…snapshot for " with nothing after it is what that used to read as.
        "state_snapshot": lambda: (
            f"Recorded a state snapshot for {div}" if div
            else "Recorded a state snapshot of the tournament"
        ),
    }
    render = templates.get(t)
    return render() if render else t.replace("_", " ").capitalize()


# The payload of a ``state_snapshot`` is a whole tournament, and a big
# tournament's runs to hundreds of kilobytes. The activity page shows the head of
# it and points at the download rather than pasting the lot into the page.
MAX_PAYLOAD_CHARS = 4000

# A compact line's ceiling. Past this a payload is not being skimmed any more.
MAX_LINE_CHARS = 300

# How many rows of one event's delta the page will render. Almost nothing
# reaches it — a results save is a handful of slips, a registration edit two or
# three — but the save that enters a 200-player field is one event with 200 rows
# in it, and that one event would otherwise be most of the page's weight. The
# rest is a count and a pointer to the download, which is the right tool for
# reading 200 rows anyway.
MAX_DETAIL_ROWS = 50


def _player_names(numbers) -> dict:
    """``{player number: name}`` for the identifiers a payload mentions."""
    from tournaments.models import Player

    wanted = {n for n in numbers if isinstance(n, str) and n}
    if not wanted:
        return {}
    return dict(
        Player.objects.filter(player_number__in=wanted).values_list(
            "player_number", "name"
        )
    )


def _delta_rows(payload) -> list:
    """Every row a delta payload mentions, both sides of a change included."""
    return [
        *payload.get("added", []),
        *payload.get("removed", []),
        *[c["to"] for c in payload.get("changed", [])],
        *[c["from"] for c in payload.get("changed", [])],
    ]


def _is_delta(payload) -> bool:
    """A payload that says what changed, rather than carrying the collection."""
    return "rows" not in payload and "added" in payload


def _grid_numbers(grid, payload):
    """The player identifiers one grid-save payload mentions."""
    return {
        row.get(field)
        for row in _delta_rows(payload)
        for field in grid.portable_player_fields
    }


def _payload_strings(value):
    """Every string a payload holds, at any depth — the candidates for a name.

    A payload identifies people the way replay needs them: by the identifier
    that survives a rename and resolves in a fresh database. A reader needs the
    name. Rather than teach the page which key holds a person for each of three
    dozen event types — and miss the nested ones, where the people are inside a
    list of refreshed entrants or a seeding of pairs — every string is offered to
    the player table and the ones that are somebody come back named.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _payload_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _payload_strings(item)


def _name_people(value, names):
    """``value`` with every string that is a player number named.

    The number is kept alongside the name: it is what the log actually recorded,
    it is what a replay acts on, and two players can share a name.
    """
    if isinstance(value, str):
        # The name alone. The number is what the log recorded and what the
        # download carries; on the page it is noise, and in a result line the
        # brackets already mean scores.
        return names.get(value, value)
    if isinstance(value, list):
        return [_name_people(item, names) for item in value]
    if isinstance(value, dict):
        return {key: _name_people(item, names) for key, item in value.items()}
    return value


def _render_value(value):
    """A payload value as the page should show it. Booleans read as yes/no —
    ``paid: False`` is a checkbox, and "no" is what the director unticked."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None or value == "":
        return "—"
    return str(value)


def _compact_value(value, names):
    """A payload value on one line, with no JSON punctuation.

    The page is read by a person looking quickly for what happened, so a list is
    a comma-separated run and a mapping is ``key=value`` pairs. The downloaded
    log is the machine-readable copy; nothing here has to parse.
    """
    if isinstance(value, dict):
        return " ".join(
            f"{key}={_compact_value(item, names)}"
            for key, item in sorted(value.items())
            if item not in (None, "", [], {})
        )
    if isinstance(value, list):
        return ", ".join(_compact_value(item, names) for item in value)
    return _render_value(_name_people(value, names))


def _already_said(value, summary) -> bool:
    """Is this value already in the line above it?

    "Published round 2 in Division 1" does not need "round 2" underneath. Word
    bounded, so a round 2 is not swallowed by a round 12.
    """
    if isinstance(value, (list, dict)) or value in (None, ""):
        return False
    return re.search(rf"\b{re.escape(str(value))}\b", summary) is not None


def _field_label(key) -> str:
    return key.replace("_", " ")


def describe_events(events) -> list:
    """``(description, detail)`` for a page of events.

    One pass, one query for the people: the page is the unit because that is
    where the cost was — a hundred rows each looking up its own handful of
    players is a hundred queries for a table that fits in one. The description is
    handed to the detail so it can leave out what the line above already said.
    """
    from tournaments.grids import GRID_BY_EVENT

    numbers = set()
    for event in events:
        grid = GRID_BY_EVENT.get(event.event_type)
        payload = event.payload or {}
        if grid is not None and _is_delta(payload):
            numbers |= _grid_numbers(grid, payload)
        elif _readable(payload):
            # A command payload names its people wherever it likes, so every
            # string in it is a candidate. Cheap: they all ride the one query.
            numbers |= set(_payload_strings(payload))
    names = _player_names(numbers)
    described = []
    for event in events:
        summary = describe_event(event, names)
        described.append((summary, event_detail(event, names, summary)))
    return described


def event_detail(event, names=None, summary="") -> dict:
    """What one event *did*, for the activity page to render under its summary.

    Two shapes. A grid save carries a delta, and is rendered row by row in the
    grid's own vocabulary — the entrant, the match — with the people named
    rather than numbered and, for an edited row, the fields that moved. Anything
    else is shown as its recorded payload: the summary line already says what the
    command was, and the payload is short and readable for every command that
    isn't a grid save.
    """
    from tournaments.grids import GRID_BY_EVENT

    payload = event.payload or {}
    grid = GRID_BY_EVENT.get(event.event_type)
    if grid is not None and "rows" in payload:
        # A grid save from before deltas: the whole collection, so there is no
        # movement to show — just what it held, in the same compact lines.
        if names is None:
            names = _player_names(
                row.get(field)
                for row in payload["rows"]
                for field in grid.portable_player_fields
            )
        rows = payload["rows"][:MAX_DETAIL_ROWS]
        lines = [
            (
                "",
                "",
                grid.portable_summary(row, names)
                or grid.portable_label(row, names)
                or "",
            )
            for row in rows
        ]
        return {"lines": lines, "more": len(payload["rows"]) - len(rows)}
    if grid is None or not _is_delta(payload):
        # A command payload: one compact line of what it recorded.
        return _recorded_detail(payload, names, summary, event.event_type)
    if names is None:
        names = _player_names(_grid_numbers(grid, payload))

    def summary_of(row):
        return (
            grid.portable_summary(row, names)
            or grid.portable_label(row, names)
            or ""
        )

    def change_of(before, after):
        # A grid with no compact form of its own says what it was and what it
        # became, on one line.
        return grid.portable_change(before, after, names) or (
            f"{summary_of(before)} → {summary_of(after)}"
        )

    # One budget across the three kinds, spent in the order the page renders
    # them, so what a reader sees first is what survives the cap.
    budget = MAX_DETAIL_ROWS
    lines = []
    for item in payload.get("changed", []):
        if budget <= 0:
            break
        lines.append(("changed", "~", change_of(item["from"], item["to"])))
        budget -= 1
    for row in payload.get("added", []):
        if budget <= 0:
            break
        lines.append(("added", "+", summary_of(row)))
        budget -= 1
    for row in payload.get("removed", []):
        if budget <= 0:
            break
        lines.append(("removed", "\u2212", summary_of(row)))
        budget -= 1
    total = sum(len(payload.get(key, [])) for key in ("added", "removed", "changed"))
    return {"lines": lines, "more": total - len(lines)}


def _readable(payload) -> bool:
    """Small enough to render for a person rather than dump.

    The one payload that is not is ``state_snapshot`` — a whole tournament,
    hundreds of kilobytes of it. Walking that to name its people would cost more
    than it is worth on a page where the snapshot is one row of a hundred, and
    its entrants carry their names already.
    """
    return len(json.dumps(payload, default=str)) <= MAX_PAYLOAD_CHARS


def _recorded_detail(payload, names=None, summary="", event_type="") -> dict:
    """A command payload as one compact line, or nothing when the line above
    already said it.

    No JSON on the page. A director skimming the log wants "round 3 · winner
    Emmanuel Egbele", not a brace-and-quote rendering of a dict they have to read
    like code. The downloaded log stays the raw replay payload, which is what a
    machine — or a person reconstructing an event — reads instead.

    ``division`` goes, and so does anything the summary already said: an event
    described as "Published round 2 in Division 1" has nothing left to unfold.
    """
    if not _readable(payload):
        # A whole-tournament snapshot. Rendering that compactly is no service to
        # anybody; say what it holds and point at the file.
        return {"note": _snapshot_note(payload)}
    if names is None:
        names = _player_names(_payload_strings(payload))
    compact = _COMPACT_COMMANDS.get(event_type)
    if compact:
        line = compact(payload, names)
        if line:
            return {"lines": [("", "", line)]}
    parts = []
    # The payload's own key order, not sorted: a command records its arguments
    # in the order they are said, and alphabetical scatters them.
    for key, value in payload.items():
        if key == "division" or value in (None, "", [], {}):
            continue
        text = _compact_value(value, names)
        # Against the *rendered* text, so a player already named in the summary
        # drops out even though the payload holds their number.
        if not text or _already_said(text, summary):
            continue
        parts.append(f"{_field_label(key)} {text}")
    line = " · ".join(parts)
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS].rsplit(" ", 1)[0] + " …"
    return {"lines": [("", "", line)]} if line else {}


def _one_result_line(payload, names) -> str:
    """``result_added`` / ``result_edited`` as a result rather than as the form
    that entered it: "R1  Emmanuel Egbele (300) – Cheryl Melvin (500) ✓".

    The single-result form is the commonest thing a director does, so its log
    entry is worth saying in the shape the results grid says one. Rendered *by*
    the grid, from the same row shape it saves, so the two cannot drift: the
    payload names the two sides of the board directly, which is what
    ``winner_started`` means.
    """
    from tournaments.grids import GRID_BY_EVENT

    winner = payload.get("winner_player")
    first, second = payload.get("first_player"), payload.get("second_player")
    loser = second if winner == first else first
    if not (winner and loser):
        return ""
    return GRID_BY_EVENT["results_saved"].portable_summary(
        {
            "round": payload.get("round"),
            "winner": winner,
            "loser": loser,
            "winner_score": payload.get("winner_score"),
            "loser_score": payload.get("loser_score"),
            "winner_started": winner == first,
        },
        names,
    )


_COMPACT_COMMANDS = {
    "result_added": _one_result_line,
    "result_edited": _one_result_line,
}


def _snapshot_note(payload) -> str:
    """One line for a payload too big to show: what it holds, and where to read
    it."""
    divisions = payload.get("divisions") or []
    entrants = sum(len(d.get("entrants") or []) for d in divisions)
    results = sum(len(d.get("results") or []) for d in divisions)
    return (
        f"A snapshot of the whole tournament — {len(divisions)} division(s), "
        f"{entrants} entrant(s), {results} result(s). Download the log to read it."
    )

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
