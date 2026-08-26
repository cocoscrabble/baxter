"""Rewrite stored event digests from the v1 (name-keyed) form to v2.

Phase 3d of plans/PLAN_PLAYER_IDENTITY.md. The state digest used to identify
players by name; it now identifies them by number, so every digest recorded
before that change hashes a vocabulary the code can no longer produce, and
``replay --verify`` would report a mismatch on every event of every existing
tournament.

**This rewrites an append-only log, so it has to earn the right to.** For each
tournament it replays the log, computes both digests at every step, and only
rewrites if the *v1* digests it computes match the ones already stored. A
tournament that does not verify was already divergent before this migration
touched it; papering over that would destroy the only evidence, so it is skipped
and reported instead.

Payloads are *not* rewritten. They stay v1 and are upgraded on read by
``replay.SCHEMA_UPGRADES``, which is what ``schema_version`` is for — only the
digest, which is a derived value rather than recorded intent, is recomputed.

The replay runs inside a savepoint that is always rolled back, so the
reconstructed tournament never survives; only the digests it produced do.
"""

from django.db import connection, transaction


def schema_mismatch() -> str | None:
    """Why the live models cannot be used against this database, or None.

    The backfill replays through the real command layer, so it needs the live
    models — which only match the database once every migration has been
    applied. Run inside a migration that is *not* last, every replay dies on a
    missing column and every tournament is silently skipped. Checking up front
    turns that into a refusal with something actionable in it.
    """
    from django.apps import apps

    tables = set(connection.introspection.table_names())
    for model in apps.get_app_config("tournaments").get_models():
        table = model._meta.db_table
        if table not in tables:
            return f"table {table!r} does not exist yet"
        columns = {
            c.name
            for c in connection.introspection.get_table_description(
                connection.cursor(), table
            )
        }
        for field in model._meta.concrete_fields:
            if field.column not in columns:
                return f"{table}.{field.column} does not exist yet"
    return None


class _Rollback(Exception):
    """Carries the collected digests out of the savepoint that is discarded."""

    def __init__(self, digests):
        super().__init__("rollback")
        self.digests = digests


def _collect(events):
    """{seq: (v1_digest, v2_digest)} for each event that names a division."""
    from tournaments.events import division_digest
    from tournaments.replay import ReplayContext, apply_event

    ctx = ReplayContext()
    out = {}
    for event in events:
        apply_event(ctx, event)
        if not (event.get("division") and ctx.tournament):
            continue
        division = ctx.tournament.divisions.filter(name=event["division"]).first()
        if division is None:
            continue
        out[event["seq"]] = (
            division_digest(division, version=1),
            division_digest(division, version=2),
        )
    return out


def replayed_digests(events):
    """Replay ``events`` in a discarded savepoint, returning the digests only."""
    try:
        with transaction.atomic():
            raise _Rollback(_collect(events))
    except _Rollback as rollback:
        return rollback.digests


def backfill_tournament(tournament):
    """Rewrite one tournament's digests. Returns (rewritten, reason).

    ``reason`` is None on success, else why the tournament was left alone.
    """
    from tournaments.replay import events_from_tournament

    events = events_from_tournament(tournament)
    stored = {e["seq"]: e["digest"] for e in events if e.get("digest")}
    if not stored:
        return 0, None

    try:
        digests = replayed_digests(events)
    except Exception as exc:  # a log that will not replay cannot be verified
        return 0, f"replay failed: {type(exc).__name__}: {exc}"

    # Already done? Every stored digest matching the v2 recomputation means a
    # previous run finished, so this is a re-run and there is nothing to do.
    # Checked *before* the v1 comparison, which would otherwise report an
    # already-backfilled tournament as divergent — technically true and
    # thoroughly misleading.
    if all(
        seq in digests and digests[seq][1] == digest
        for seq, digest in stored.items()
    ):
        return 0, None

    diverged = [
        seq
        for seq, digest in stored.items()
        if seq not in digests or digests[seq][0] != digest
    ]
    if diverged:
        return 0, (
            f"{len(diverged)} of {len(stored)} digests do not reproduce under v1 "
            f"(first at seq {min(diverged)}) — left untouched"
        )

    from tournaments.models import TournamentEvent

    updated = []
    for event in tournament.events.filter(seq__in=stored):
        event.digest = digests[event.seq][1]
        updated.append(event)
    TournamentEvent.objects.bulk_update(updated, ["digest"], batch_size=500)
    return len(updated), None


def backfill_all(log=print):
    """Backfill every tournament, reporting per tournament. Returns (done, skipped)."""
    from tournaments.models import Tournament

    done = skipped = 0
    for tournament in Tournament.objects.order_by("pk"):
        count, reason = backfill_tournament(tournament)
        if reason:
            skipped += 1
            log(f"  SKIPPED {tournament.name!r}: {reason}")
        elif count:
            done += 1
            log(f"  {tournament.name!r}: {count} digest(s) rewritten")
    return done, skipped
