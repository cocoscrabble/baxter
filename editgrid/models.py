"""Reusable editable-grid persistence: optimistic-concurrency tokens and
soft editing-presence, both keyed by an opaque string the host app supplies.

This app is deliberately domain-agnostic — it knows nothing about the models
being edited. A host (e.g. tournaments) composes a ``key`` that identifies one
editable collection (for Baxter: ``f"division:{pk}:{scope}"``) and passes it in.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# How long after the last heartbeat an editor is still considered present
# (two missed beats at the client's 15s cadence).
PRESENCE_WINDOW = timedelta(seconds=30)


class EditVersion(models.Model):
    """Optimistic-concurrency token for one editable collection.

    Grids that save by wiping and recreating their whole collection would let
    concurrent editors silently clobber each other. The edit page embeds the
    current version for its ``key``; a save is rejected if the version has moved
    on since the page loaded. One row per key.
    """

    key = models.CharField(max_length=200, unique=True)
    version = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.key} @ v{self.version}"

    @classmethod
    def version_for(cls, key):
        """Current version for a key, or 0 if it has never been saved."""
        row = cls.objects.filter(key=key).first()
        return row.version if row else 0

    @classmethod
    def lock(cls, key):
        """Fetch-or-create the version row with a row lock.

        Must be called inside a ``transaction.atomic`` block; the lock
        serializes concurrent saves of the same key.
        """
        return cls.objects.select_for_update().get_or_create(key=key)[0]


class EditPresence(models.Model):
    """Tracks who currently has an editable collection open.

    Each open edit page heartbeats periodically, refreshing its row's
    ``last_seen``. An editor is present while their last heartbeat is within
    ``PRESENCE_WINDOW``; stale rows are pruned opportunistically. Drives a soft
    "someone else is editing this" banner — the actual clobber protection is
    :class:`EditVersion`. One row per (key, user).
    """

    key = models.CharField(max_length=200)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="edit_presences"
    )
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["key", "user"]

    def __str__(self):
        return f"{self.user} editing {self.key}"

    @classmethod
    def heartbeat(cls, key, user):
        """Record (or refresh) this user's presence, then prune stale rows."""
        cls.objects.update_or_create(key=key, user=user)
        cls.prune(key)

    @classmethod
    def release(cls, key, user):
        """Drop this user's presence (e.g. when their tab closes)."""
        cls.objects.filter(key=key, user=user).delete()

    @classmethod
    def prune(cls, key):
        """Delete presence rows whose last heartbeat is older than the window."""
        cutoff = timezone.now() - PRESENCE_WINDOW
        cls.objects.filter(key=key, last_seen__lt=cutoff).delete()

    @classmethod
    def others(cls, key, user):
        """Usernames of other editors active within the window, sorted."""
        cutoff = timezone.now() - PRESENCE_WINDOW
        return sorted(
            cls.objects.filter(key=key, last_seen__gte=cutoff)
            .exclude(user=user)
            .values_list("user__username", flat=True)
        )
