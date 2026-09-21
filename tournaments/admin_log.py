"""Recording site-wide admin actions: the admin log (``/manage/log/``).

Every admin action runs inside ``logged``::

    with logged(AdminAction.WESPA_LINK, request.user) as entry:
        try:
            link_player(player, row)
        except ValueError as exc:
            entry.fail(str(exc))
            ...
        entry.summary = f"Linked {player.name} to WESPA {row.name}"

and leaves exactly one row, whichever way it ends:

- normally — succeeded, with ``entry.summary``;
- after ``entry.fail(...)`` — an expected refusal the caller handled;
- by an exception — failed, with the exception, which is then re-raised. That
  is the case the log is most for: a scheduled pull that crashed has nobody
  watching its traceback.

The row is written after the block, so it survives the rollback of an atomic
action that failed inside it. Admin views run outside a request transaction
(no ``ATOMIC_REQUESTS``); calling this inside an outer ``atomic`` would put the
failure row in the same rollback.

``test_admin_log.CompletenessTests`` fails if an admin-only view with a POST
handler never calls this.
"""

from contextlib import contextmanager
from dataclasses import dataclass

from .models import AdminAction


@dataclass
class Entry:
    summary: str = ""
    error: str = ""
    ok: bool = True

    def fail(self, error):
        self.ok = False
        self.error = str(error)


def record(kind, actor=None, *, ok, summary="", error=""):
    """Write one row. ``actor`` None means nobody — a scheduled run."""
    return AdminAction.objects.create(
        kind=kind,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_name=actor.get_username() if getattr(actor, "is_authenticated", False) else "",
        ok=ok,
        summary=summary,
        error=error,
    )


@contextmanager
def logged(kind, actor=None):
    entry = Entry()
    try:
        yield entry
    except Exception as exc:
        record(
            kind, actor, ok=False, summary=entry.summary,
            error=f"Unexpected error: {type(exc).__name__}: {exc}",
        )
        raise
    record(kind, actor, ok=entry.ok, summary=entry.summary, error=entry.error)
