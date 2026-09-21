"""The admin log: every site-wide admin action, and whether it worked.

The point of the log is the actions nobody watched — a scheduled pull that
failed, or crashed — so the outcomes are what is pinned hardest. Completeness
is pinned the same way the admin index pins its links: an admin page that can
change something and never writes here would be a hole in the record nobody
notices.
"""

import inspect
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from tournaments.models import AdminAction, Player, RosterSync, WespaPlayer, WespaSync
from tournaments.roster_sync import run_sync
from tournaments import wespa_sync
from users.models import User

from .test_admin_index import admin_only_urls
from .test_roster_import import entry, roster
from .test_roster_sync import refused, served
from .test_wespa import document, row


class CompletenessTests(TestCase):
    def test_every_admin_page_that_posts_writes_to_the_log(self):
        from django.urls import resolve

        silent = []
        for name, url in admin_only_urls().items():
            view = resolve(url).func.view_class
            if not hasattr(view, "post"):
                continue
            source = inspect.getsource(view)
            # run_sync logs for itself; everything else wraps its action.
            if "logged(" not in source and "run_sync(" not in source:
                silent.append(name)
        # The password page takes an argument, so the URL sweep cannot see it.
        from tournaments.views import UserSetPasswordView

        self.assertIn("logged(", inspect.getsource(UserSetPasswordView))
        self.assertEqual(silent, [], "Wrap the action in admin_log.logged.")


class PullTests(TestCase):
    def test_a_scheduled_pull_is_logged_with_nobody_as_the_actor(self):
        with served(roster(entry("0233", "Alec"))):
            run_sync(RosterSync.SCHEDULED)
        action = AdminAction.objects.get()
        self.assertEqual(
            (action.kind, action.ok, action.actor, action.actor_name),
            (AdminAction.ROSTER_PULL, True, None, ""),
        )
        self.assertIn("Scheduled: Pulled 1 player(s)", action.summary)

    def test_a_refused_pull_is_logged_as_failed_with_the_reason(self):
        with refused("The central database rejected the token."):
            run_sync(RosterSync.SCHEDULED)
        action = AdminAction.objects.get()
        self.assertFalse(action.ok)
        self.assertEqual(action.error, "The central database rejected the token.")

    def test_a_crashed_pull_is_logged_and_still_raises(self):
        with served(roster(entry("0233", "Alec"))), patch(
            "tournaments.roster_sync.import_roster",
            side_effect=RuntimeError("disk full"),
        ):
            with self.assertRaises(RuntimeError):
                run_sync(RosterSync.SCHEDULED)
        action = AdminAction.objects.get()
        self.assertFalse(action.ok)
        self.assertIn("RuntimeError: disk full", action.error)

    def test_a_hand_pull_names_who_ran_it(self):
        admin = User.objects.create_user(username="admin-log", password="pw", role="admin")
        with patch(
            "tournaments.wespa_sync.fetch_wespa",
            return_value=document(row(7, "Bea Fox", 1450)),
        ):
            wespa_sync.run_sync(WespaSync.MANUAL, actor=admin)
        action = AdminAction.objects.get()
        self.assertEqual((action.kind, action.actor, action.ok),
                         (AdminAction.WESPA_PULL, admin, True))


class ViewActionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-log", password="pw", role="admin"
        )
        self.client.force_login(self.admin)

    def test_a_refused_link_is_logged_as_failed(self):
        taken = WespaPlayer.objects.create(wespa_id=7, name="Bea Fox", rating=1450)
        Player.objects.create(name="Bea", player_number="0001", rating=0, wespa_id=7)
        other = Player.objects.create(name="Bea Fox", player_number="0002", rating=0)
        self.client.post(reverse("wespa_import"), {
            "action": "link", "player_number": other.player_number,
            "wespa_id": taken.wespa_id,
        })
        action = AdminAction.objects.get()
        self.assertEqual((action.kind, action.ok), (AdminAction.WESPA_LINK, False))
        self.assertIn("already linked", action.error)

    def test_a_rejected_player_import_is_logged_as_failed(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.post(reverse("player_import"), {
            "players_file": SimpleUploadedFile("players.json", b"not json"),
        })
        action = AdminAction.objects.get()
        self.assertEqual((action.kind, action.ok), (AdminAction.PLAYER_IMPORT, False))
        self.assertIn("not valid JSON", action.error)

    def test_a_password_set_is_logged_without_the_password(self):
        director = User.objects.create_user(username="td-log", password="pw")
        self.client.post(reverse("user_set_password", args=[director.pk]), {
            "new_password1": "Zebra-crossing-42", "new_password2": "Zebra-crossing-42",
        })
        action = AdminAction.objects.get()
        self.assertEqual((action.kind, action.ok), (AdminAction.PASSWORD_SET, True))
        self.assertIn("td-log", action.summary)
        self.assertNotIn("Zebra", action.summary + action.error)

    def test_a_mistyped_password_is_not_an_action(self):
        director = User.objects.create_user(username="td-log", password="pw")
        self.client.post(reverse("user_set_password", args=[director.pk]), {
            "new_password1": "Zebra-crossing-42", "new_password2": "different",
        })
        self.assertFalse(AdminAction.objects.exists())


class PageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-log", password="pw", role="admin"
        )
        AdminAction.objects.create(
            kind=AdminAction.ROSTER_PULL, ok=True, summary="Scheduled: fine"
        )
        AdminAction.objects.create(
            kind=AdminAction.WESPA_PULL, ok=False, summary="Scheduled",
            error="The WESPA list could not be reached.",
        )

    def test_a_director_is_refused(self):
        director = User.objects.create_user(username="td-log", password="pw")
        self.client.force_login(director)
        self.assertEqual(self.client.get(reverse("admin_log")).status_code, 403)

    def test_it_shows_outcomes(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_log"))
        self.assertContains(response, "Scheduled: fine")
        self.assertContains(response, "The WESPA list could not be reached.")
        self.assertContains(response, "Failed")

    def test_failures_only(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_log"), {"failed": "1"})
        self.assertNotContains(response, "Scheduled: fine")
        self.assertContains(response, "could not be reached")

    def test_the_index_flags_a_recent_failure(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_index"))
        self.assertContains(response, "failed in the last week")
