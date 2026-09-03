"""The scheduled roster pull (plans/PLAN_COCO_PROGRAM.md, W4).

The pull itself is covered by ``test_roster_import``; what is new here is that
it runs with nobody watching. So these are about what survives a run that no
human saw: the record it leaves, the held-back rows a director has to find
later, and a failure making itself visible rather than repeating quietly every
six hours.
"""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from tournaments.models import Entrant, Player, RosterSync
from tournaments.roster_import import RosterFetchError
from tournaments.roster_sync import pending_resolutions, run_sync
from users.models import User

from .test_roster_import import entry, roster

def served(doc):
    """Patch the fetch to return ``doc``."""
    return patch(
        "tournaments.roster_sync.fetch_roster",
        return_value=json.dumps(doc).encode(),
    )


def refused(message="The central database rejected the token."):
    return patch(
        "tournaments.roster_sync.fetch_roster",
        side_effect=RosterFetchError(message),
    )


class RunSyncTests(TestCase):
    def test_a_good_pull_applies_it_and_records_what_it_did(self):
        Player.objects.create(name="Alec", player_number="0233", rating=1500)
        with served(roster(entry("0233", "Alec", rating=1700),
                           entry("0301", "Nellie"))):
            record = run_sync(RosterSync.SCHEDULED)

        self.assertTrue(record.ok)
        self.assertEqual((record.added, record.updated), (1, 1))
        self.assertEqual(record.generated_at, "2026-08-22T14:03:00Z")
        self.assertEqual(Player.objects.get(player_number="0233").rating, 1700)

    def test_a_failed_fetch_is_recorded_rather_than_raised(self):
        # A scheduled pull meeting a rotated token is an ordinary event, not a
        # traceback: it has to leave something a human can read later.
        with refused():
            record = run_sync(RosterSync.SCHEDULED)

        self.assertFalse(record.ok)
        self.assertIn("rejected the token", record.error)
        self.assertEqual(record.pk and RosterSync.objects.count(), 1)

    def test_a_failed_pull_changes_no_players(self):
        with refused():
            run_sync(RosterSync.SCHEDULED)
        self.assertEqual(Player.objects.count(), 0)

    def test_a_malformed_document_is_recorded_too(self):
        doc = roster(entry("0233", "Alec"))
        doc["schema"] = "coco.roster/2"
        with served(doc):
            record = run_sync(RosterSync.SCHEDULED)

        self.assertFalse(record.ok)
        self.assertIn("coco.roster/2", record.error)
        self.assertEqual(Player.objects.count(), 0)

    def test_an_uploaded_snapshot_needs_no_endpoint(self):
        raw = json.dumps(roster(entry("0233", "Alec"))).encode()
        record = run_sync(RosterSync.UPLOAD, raw)
        self.assertTrue(record.ok)
        self.assertEqual(record.added, 1)

    def test_the_summary_reads_as_a_sentence(self):
        with served(roster(entry("0233", "Alec"))):
            record = run_sync(RosterSync.SCHEDULED)
        self.assertIn("1 added", record.summary())

        with refused("Could not reach cocodb.example: timed out."):
            failure = run_sync(RosterSync.SCHEDULED)
        self.assertIn("Could not reach", failure.summary())


class HeldBackSurvivalTests(TestCase):
    """The rows a pull cannot resolve on its own have to outlive the run.

    They used to live in the puller's session, which a cron tick does not have.
    """

    def setUp(self):
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )
        self.doc = roster(entry("0301", "Joe Thorngren", rating=1400))

    def test_a_scheduled_pull_holds_them_back_and_keeps_them(self):
        with served(self.doc):
            record = run_sync(RosterSync.SCHEDULED)

        self.assertEqual(len(record.pending), 1)
        # Nothing was written: still one player, still on the T- number.
        self.assertEqual(Player.objects.count(), 1)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")

        # And a director coming along afterwards can still see it.
        held = pending_resolutions()
        self.assertEqual(len(held), 1)
        self.assertEqual((held[0].local_number, held[0].roster_number),
                         ("T-4", "0301"))

    def test_a_later_failure_does_not_hide_them(self):
        with served(self.doc):
            run_sync(RosterSync.SCHEDULED)
        with refused():
            run_sync(RosterSync.SCHEDULED)

        self.assertEqual(len(pending_resolutions()), 1,
                         "the last good pull still has something to say")

    def test_a_later_good_pull_supersedes_them(self):
        with served(self.doc):
            run_sync(RosterSync.SCHEDULED)
        # The guest has been added to Baxter under their real number in the
        # meantime, so the next pull has nothing to hold back.
        self.guest.player_number = "0301"
        self.guest.is_provisional = False
        self.guest.save()
        with served(self.doc):
            run_sync(RosterSync.SCHEDULED)

        self.assertEqual(pending_resolutions(), [])


class ConfirmingWhatCronFoundTests(TestCase):
    """A director confirms a resolution the scheduled pull left behind."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-sync", password="pw", role="admin", is_staff=True
        )
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )
        with served(roster(entry("0301", "Joe Thorngren", rating=1400))):
            run_sync(RosterSync.SCHEDULED)
        self.url = reverse("roster_import")
        self.client.force_login(self.admin)

    def test_the_page_offers_it_without_pulling_again(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Needs confirming")
        self.assertContains(response, "T-4")

    def test_confirming_moves_the_player_and_clears_the_entry(self):
        response = self.client.post(
            self.url, {"action": "resolve", "pending": "T-4:0301"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "0301")
        self.assertEqual(self.guest.rating, 1400)
        self.assertFalse(self.guest.is_provisional)
        # The record no longer offers it, so it cannot be confirmed twice.
        self.assertEqual(pending_resolutions(), [])
        self.assertNotContains(response, "Needs confirming")

    def test_a_second_admin_sees_the_same_pending_row(self):
        # It lives on the record, not in whoever's session pulled it — and for
        # a scheduled pull there is no session at all.
        other = User.objects.create_user(
            username="admin-2", password="pw", role="admin", is_staff=True
        )
        self.client.force_login(other)
        self.assertContains(self.client.get(self.url), "Needs confirming")


class PageStatusTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-status", password="pw", role="admin", is_staff=True
        )
        self.client.force_login(self.admin)
        self.url = reverse("roster_import")

    def test_it_says_when_nothing_has_run(self):
        self.assertContains(self.client.get(self.url), "No pull has run yet")

    def test_a_failing_scheduled_pull_is_visible(self):
        # The whole point of the record: without it this is invisible until
        # someone notices the ratings are stale.
        with refused():
            run_sync(RosterSync.SCHEDULED)
        response = self.client.get(self.url)
        self.assertContains(response, "failed")
        self.assertContains(response, "rejected the token")

    def test_a_good_pull_reports_its_counts(self):
        with served(roster(entry("0233", "Alec"))):
            run_sync(RosterSync.SCHEDULED)
        self.assertContains(self.client.get(self.url), "1 added")


class CommandTests(TestCase):
    def _run(self, *args, **kwargs):
        out = StringIO()
        call_command("pull_roster", *args, stdout=out, stderr=StringIO(), **kwargs)
        return out.getvalue()

    def test_it_pulls_and_reports(self):
        with served(roster(entry("0233", "Alec"))):
            output = self._run()
        self.assertIn("1 added", output)
        self.assertTrue(Player.objects.filter(player_number="0233").exists())

    def test_it_records_the_run_as_scheduled(self):
        with served(roster(entry("0233", "Alec"))):
            self._run()
        self.assertEqual(RosterSync.latest().source, RosterSync.SCHEDULED)

    def test_a_failure_exits_non_zero_and_still_leaves_a_record(self):
        # Non-zero is what makes cron notice; the record is what makes a human
        # notice. Both, not either.
        with refused(), self.assertRaises(CommandError):
            self._run()
        record = RosterSync.latest()
        self.assertFalse(record.ok)
        self.assertIn("rejected the token", record.error)

    def test_held_back_rows_are_not_a_failure_but_are_named(self):
        Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )
        with served(roster(entry("0301", "Joe Thorngren"))):
            output = self._run()
        self.assertIn("Joe Thorngren", output)
        self.assertIn("T-4", output)
        self.assertIn("0301", output)

    def test_a_file_can_be_imported_without_an_endpoint(self):
        path = self._write(roster(entry("0233", "Alec")))
        output = self._run("--file", path)
        self.assertIn("1 added", output)
        self.assertEqual(RosterSync.latest().source, RosterSync.UPLOAD)

    def test_a_missing_file_says_so(self):
        with self.assertRaisesRegex(CommandError, "Could not read"):
            self._run("--file", "/nonexistent/roster.json")

    def _write(self, doc):
        import tempfile

        f = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(doc, f)
        f.close()
        self.addCleanup(lambda: __import__("os").unlink(f.name))
        return f.name


class ScheduleTests(TestCase):
    """app.json is what Dokku installs into the host crontab on deploy.

    It names the command as a string, so nothing but a test connects the two:
    renaming the command would leave a cron entry that fails four times a day
    against a deployed app, which is exactly the kind of silence this whole
    feature exists to remove.
    """

    def _app_json(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "app.json"
        return json.loads(path.read_text())

    def test_the_cron_command_exists(self):
        from django.core.management import get_commands

        entry = self._app_json()["cron"][0]
        # "uv run manage.py pull_roster" -> "pull_roster"
        name = entry["command"].split()[-1]
        self.assertIn(name, get_commands())

    def test_the_schedule_is_a_five_field_cron_expression(self):
        schedule = self._app_json()["cron"][0]["schedule"]
        self.assertEqual(len(schedule.split()), 5, schedule)

    def test_it_does_not_resync_the_environment_on_every_tick(self):
        # The image already has the venv. Without --no-sync, uv rebuilds the
        # Rust extension inside every one-off container -- the Dockerfile's
        # `COPY . .` lands fresh mtimes on the crate sources after `uv sync`,
        # so uv sees the path dependency as stale each time. Observed on the
        # first production run: "Built scrabble-pairing-py", four times a day,
        # putting a maturin build in the failure path of a job that otherwise
        # only touches HTTP and the database.
        command = self._app_json()["cron"][0]["command"]
        self.assertIn("--no-sync", command)


class LiveTournamentTests(TestCase):
    """A pull cannot move an event that is already under way — scheduled or not.

    This is governing principle 1 restated where it now matters most: the pull
    used to happen only when a human chose the moment, and now it happens on a
    timer that knows nothing about who is mid-round.
    """

    def test_a_scheduled_pull_does_not_move_a_registered_entrant(self):
        from tournaments.commands import create_tournament

        owner = User.objects.create_user(username="td-cron", password="pw")
        player = Player.objects.create(
            name="Alec", player_number="0233", rating=1500, deviation=80.0,
            career_games=100,
        )
        tournament = create_tournament(
            None, owner,
            {
                "name": "Live", "location": "X", "start_date": "2026-05-01",
                "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
            },
        )
        entrant = Entrant.enter(tournament.divisions.get(), player, 1)
        before = (entrant.rating, entrant.deviation, entrant.career_games)

        with served(roster(entry("0233", "Alec", rating=1900, deviation=40.0,
                                 games=500))):
            run_sync(RosterSync.SCHEDULED)

        entrant.refresh_from_db()
        self.assertEqual(
            (entrant.rating, entrant.deviation, entrant.career_games), before
        )
        # The player row did move — it is the entrant's frozen seed that must not.
        self.assertEqual(Player.objects.get(player_number="0233").rating, 1900)
