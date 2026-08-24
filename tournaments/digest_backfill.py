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

from django.db import transaction


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
