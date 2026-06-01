from django.test import TestCase
from django.utils import timezone

from editgrid.models import PRESENCE_WINDOW, EditPresence, EditVersion
from users.models import User

KEY = "thing:1:rows"


class EditVersionTests(TestCase):
    def test_version_for_defaults_to_zero(self):
        self.assertEqual(EditVersion.version_for("absent:0:x"), 0)

    def test_version_for_returns_saved_value(self):
        EditVersion.objects.create(key=KEY, version=5)
        self.assertEqual(EditVersion.version_for(KEY), 5)

    def test_lock_creates_then_returns_same_row(self):
        first = EditVersion.lock(KEY)
        second = EditVersion.lock(KEY)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EditVersion.objects.filter(key=KEY).count(), 1)


class EditPresenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = User.objects.create_user(username="a", password="x")
        cls.b = User.objects.create_user(username="b", password="x")

    def _stale(self, presence):
        old = timezone.now() - PRESENCE_WINDOW - timezone.timedelta(seconds=1)
        EditPresence.objects.filter(pk=presence.pk).update(last_seen=old)

    def test_heartbeat_upserts_one_row_per_user(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.heartbeat(KEY, self.a)
        self.assertEqual(EditPresence.objects.filter(key=KEY, user=self.a).count(), 1)

    def test_others_excludes_self_and_lists_others(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.heartbeat(KEY, self.b)
        self.assertEqual(EditPresence.others(KEY, self.a), ["b"])
        self.assertEqual(EditPresence.others(KEY, self.b), ["a"])

    def test_others_ignores_stale_and_other_keys(self):
        stale = EditPresence.objects.create(key=KEY, user=self.b)
        self._stale(stale)
        EditPresence.heartbeat("thing:1:other", self.b)
        self.assertEqual(EditPresence.others(KEY, self.a), [])

    def test_heartbeat_prunes_stale_rows(self):
        stale = EditPresence.objects.create(key=KEY, user=self.b)
        self._stale(stale)
        EditPresence.heartbeat(KEY, self.a)
        self.assertFalse(EditPresence.objects.filter(pk=stale.pk).exists())

    def test_release_drops_the_users_row(self):
        EditPresence.heartbeat(KEY, self.a)
        EditPresence.release(KEY, self.a)
        self.assertFalse(EditPresence.objects.filter(key=KEY, user=self.a).exists())
