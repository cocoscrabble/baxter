"""The whole-database export on /manage/: who may take it, what it leaves out,
and that it restores into an empty database as the same tournaments."""

import gzip
import json
import os
import tempfile

from django.contrib.auth.hashers import is_password_usable
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from tournaments.db_export import export_database
from tournaments.events import division_digest
from tournaments.fuzz import Fuzzer
from tournaments.models import AdminAction, Division, Entrant
from users.models import User


def fixture_rows(data):
    return json.loads(gzip.decompress(data))


def digests():
    return {
        (d.tournament.name, d.name): division_digest(d)
        for d in Division.all_objects.select_related("tournament")
    }


class AccessTests(TestCase):
    def setUp(self):
        self.url = reverse("database_export")

    def test_a_director_is_refused(self):
        self.client.force_login(
            User.objects.create_user(username="td-x", password="pw", role="director")
        )
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_a_get_does_not_export(self):
        # A link prefetch must not be able to dump every table.
        self.client.force_login(
            User.objects.create_user(username="ad-get", password="pw", role="admin")
        )
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertFalse(AdminAction.objects.exists())

    def test_an_admin_gets_a_restorable_file_and_a_log_row(self):
        admin = User.objects.create_user(username="ad-x", password="pw", role="admin")
        self.client.force_login(admin)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response["Content-Disposition"], r'baxter-db-\d{8}-\d{4}-.+\.json\.gz"'
        )
        rows = fixture_rows(response.content)
        self.assertIn("ad-x", [r["fields"].get("username") for r in rows])
        action = AdminAction.objects.get()
        self.assertEqual(
            (action.kind, action.ok, action.actor), (AdminAction.DATABASE_EXPORT, True, admin)
        )


class ContentTests(TestCase):
    def test_no_password_hash_leaves_the_server(self):
        user = User.objects.create_user(username="secret", password="hunter2")
        rows = fixture_rows(export_database()[0])
        (row,) = [r for r in rows if r["model"] == "users.user"]
        self.assertFalse(is_password_usable(row["fields"]["password"]))
        self.assertNotIn(user.password, json.dumps(rows))

    def test_rows_the_default_managers_hide_are_exported(self):
        Fuzzer(1).run(steps=5)
        Division.all_objects.first().bye_entrant()
        hidden = Entrant.all_objects.filter(player__is_bye=True)
        self.assertFalse(Entrant.objects.filter(pk__in=hidden).exists())
        exported = {
            r["pk"] for r in fixture_rows(export_database()[0])
            if r["model"] == "tournaments.entrant"
        }
        self.assertTrue(set(hidden.values_list("pk", flat=True)) <= exported)


class RoundTripTests(TestCase):
    def test_a_restore_reproduces_every_division(self):
        for seed in (2, 3):
            Fuzzer(seed).run(steps=20)
        before = digests()
        self.assertTrue(before)
        data, _ = export_database()

        call_command("flush", interactive=False, verbosity=0)
        self.assertFalse(Division.all_objects.exists())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "db.json.gz")
            with open(path, "wb") as f:
                f.write(data)
            call_command("loaddata", path, verbosity=0)

        self.assertEqual(digests(), before)
