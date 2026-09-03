"""Running a roster pull and recording what it did.

``roster_import`` knows how to fetch and how to upsert; this is the thin layer
that does both and writes a :class:`~tournaments.models.RosterSync` row for it.
It exists because the pull now has two callers with nothing in common — an admin
clicking a button, and a cron tick with no session, no request and nobody
reading its stdout — and the difference between them must not be a difference in
what gets recorded.

**Expected failures are recorded, not raised.** A central database that is down,
or a token that has been rotated out from under us, is a normal thing for a
scheduled pull to meet; it should leave a legible row behind rather than a
traceback in a log nobody reads. Unexpected exceptions still propagate.

Nothing here decides anything about players — ``import_roster`` remains the only
code that writes to the ``Player`` table, and it is as atomic as it ever was.
"""

import logging

from .models import RosterSync
from .roster_import import (
    PendingResolution,
    RosterFetchError,
    RosterParseError,
    fetch_roster,
    import_roster,
)

logger = logging.getLogger(__name__)


def run_sync(source, raw=None) -> RosterSync:
    """Pull the roster (or import ``raw``), apply it, and record the outcome.

    ``source`` is one of the :class:`RosterSync` source constants. Pass ``raw``
    for an uploaded snapshot; leave it out to fetch from the configured
    endpoint. Returns the saved record either way — check ``record.ok``.
    """
    record = RosterSync(source=source)
    try:
        if raw is None:
            raw = fetch_roster()
        result = import_roster(raw)
    except (RosterFetchError, RosterParseError) as exc:
        record.error = str(exc)
        record.save()
        logger.warning("Roster pull failed (%s): %s", source, exc)
        return record

    record.ok = True
    record.generated_at = result.generated_at
    record.added = len(result.added)
    record.updated = len(result.updated)
    record.unchanged = len(result.unchanged)
    record.pending = [p.to_json() for p in result.pending]
    record.save()
    logger.info("Roster pull (%s): %s", source, record.summary())
    return record


def pending_resolutions(record=None):
    """The held-back rows awaiting confirmation, as ``PendingResolution`` objects.

    Read from the last *successful* pull, so a failed one does not hide what the
    last good one found.
    """
    record = record or RosterSync.latest_successful()
    if record is None:
        return []
    return [PendingResolution.from_json(entry) for entry in record.pending]


def forget_resolution(record, key):
    """Drop one confirmed resolution from ``record``, leaving the rest.

    Matched through ``PendingResolution.key`` rather than by reassembling the
    key here, so there is one definition of what identifies a resolution.
    """
    remaining = [e for e in record.pending
                 if PendingResolution.from_json(e).key != key]
    if len(remaining) != len(record.pending):
        record.pending = remaining
        record.save(update_fields=["pending"])
    return record
