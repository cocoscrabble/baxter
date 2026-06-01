from django.db import transaction
from django.http import JsonResponse

from .models import EditVersion


class check_conflict:
    """Wrap the optimistic-concurrency dance around a bulk-replace save.

    Opens a transaction, row-locks the key's version, and compares it against
    the client's. The caller checks ``guard.conflict`` (a 409 ``JsonResponse``
    when the client's version is stale, else None) and runs the save logic only
    when it's clear; on a clean exit the version is bumped and ``guard.response``
    holds the success ``JsonResponse`` to return. A ``client_version`` of None
    skips the check (e.g. an older page that doesn't send one)::

        with check_conflict(key, client_version) as guard:
            if guard.conflict:
                return guard.conflict
            ...  # the specific save logic
        return guard.response

    The transaction means an exception in the body rolls back the bump too.
    """

    def __init__(self, key, client_version):
        self.key = key
        self.client_version = client_version
        self.conflict = None
        self.response = None

    def __enter__(self):
        self._atomic = transaction.atomic()
        self._atomic.__enter__()
        self.version_row = EditVersion.lock(self.key)
        if (
            self.client_version is not None
            and self.version_row.version != self.client_version
        ):
            self.conflict = JsonResponse(
                {
                    "ok": False,
                    "conflict": True,
                    "errors": [
                        "Someone else changed this data since you opened the page. "
                        "Reload to see their changes before saving."
                    ],
                },
                status=409,
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.conflict is None:
            self.version_row.version += 1
            self.version_row.save(update_fields=["version"])
            self.response = JsonResponse(
                {"ok": True, "version": self.version_row.version}
            )
        self._atomic.__exit__(exc_type, exc, tb)
        return False
