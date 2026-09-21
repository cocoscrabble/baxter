"""Running a WESPA pull and recording what it did.

``wespa_api`` knows how to fetch, ``wespa_ratings`` knows how to apply; this is
the thin layer that does both and writes a :class:`~tournaments.models.WespaSync`
row for it. It exists for the reason ``roster_sync`` exists: the pull has two
callers with nothing in common — an admin clicking a button, and a cron tick with
no session, no request and nobody reading its stdout — and the difference between
them must not be a difference in what gets recorded.

**Expected failures are recorded, not raised.** The list is a third party's
mirror; it going away, or changing shape, is a normal thing for a scheduled pull
to meet, and it should leave a legible row behind rather than a traceback in a
log nobody reads. Unexpected exceptions still propagate.
"""

import logging

from .admin_log import logged
from .models import AdminAction, WespaSync
from .wespa_api import WespaFetchError, WespaParseError, fetch_wespa
from .wespa_ratings import PendingLink, import_wespa

logger = logging.getLogger(__name__)


def run_sync(source, raw=None, actor=None) -> WespaSync:
    """Pull the WESPA list (or import ``raw``), apply it, and record the outcome.

    ``source`` is one of the :class:`WespaSync` source constants. Pass ``raw``
    for an uploaded file; leave it out to fetch from the configured endpoint.
    Returns the saved record either way — check ``record.ok``.

    Also lands a row in the admin log, attributed to ``actor`` (None for the
    scheduled pull) — including when the pull crashes outright.
    """
    with logged(AdminAction.WESPA_PULL, actor) as entry:
        record = _run_sync(source, raw)
        if record.ok:
            entry.summary = f"{record.get_source_display()}: {record.summary()}"
        else:
            entry.summary = record.get_source_display()
            entry.fail(record.error)
    return record


def _run_sync(source, raw):
    record = WespaSync(source=source)
    try:
        if raw is None:
            raw = fetch_wespa()
        result = import_wespa(raw)
    except (WespaFetchError, WespaParseError) as exc:
        record.error = str(exc)
        record.save()
        logger.warning("WESPA pull failed (%s): %s", source, exc)
        return record

    record.ok = True
    record.added = len(result.added)
    record.updated = len(result.updated)
    record.unchanged = len(result.unchanged)
    record.rated = len(result.rated)
    record.linked = len(result.linked)
    record.pending = [p.to_json() for p in result.pending]
    record.save()
    logger.info("WESPA pull (%s): %s", source, record.summary())
    return record


def pending_links(record=None):
    """The held-back names awaiting confirmation, as ``PendingLink`` objects.

    Read from the last *successful* pull, so a failed one does not hide what the
    last good one found.
    """
    record = record or WespaSync.latest_successful()
    if record is None:
        return []
    return [PendingLink.from_json(entry) for entry in record.pending]


def forget_link(record, key):
    """Drop one resolved name from ``record``, leaving the rest.

    Matched through ``PendingLink.key`` rather than by reassembling the key here,
    so there is one definition of what identifies a pending link.
    """
    remaining = [e for e in record.pending
                 if PendingLink.from_json(e).key != key]
    if len(remaining) != len(record.pending):
        record.pending = remaining
        record.save(update_fields=["pending"])
    return record
