"""What-if import view: auth, both formats end-to-end, and error paths."""

import json

from datetime import date

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.models import Division, Player, Tournament
from users.models import User


def _bundle():
    return json.dumps({
        "name": "Nationals 2019", "location": "Reno", "start_date": "2019-07-04",
        "players": [
            {"player_number": "P1", "name": "Alice", "rating": 1600, "provisional": False},
            {"player_number": "P2", "name": "Bob", "rating": 1500, "provisional": False},
        ],
        "divisions": [{
            "name": "Open",
            "entrants": [
                {"number": 1, "player_number": "P1"},
                {"number": 2, "player_number": "P2"},
            ],
            "results": [{
                "round": 1, "winner": "P1", "winner_score": 420,
                "loser": "P2", "loser_score": 388, "winner_started": True,
            }],
        }],
        "event_log": [],
    })


class WhatIfImportViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dir", password="pw")
        self.url = reverse("whatif_import")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_get_renders_form(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import a division for what-if")

    def test_import_json_bundle_creates_sandbox(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": "", "pasted": _bundle()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Imported")

        tournament = Tournament.objects.get(is_fake=True)
        self.assertEqual(tournament.owner, self.user)
        division = tournament.divisions.get(name="Open")
        self.assertTrue(division.is_test)
        self.assertEqual(division.entrants.count(), 2)
        self.assertEqual(division.result_slips.count(), 1)
        # It logged the import as a command.
        self.assertTrue(tournament.events.filter(event_type="division_imported").exists())

    def test_import_csv_creates_sandbox(self):
        Player.objects.create(name="Alice", player_number="1", rating=1600)
        Player.objects.create(name="Bob", player_number="2", rating=1500)
        self.client.force_login(self.user)
        csv = ("Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
               ",1,Alice,420,Bob,388\n")
        response = self.client.post(self.url, {"name": "CSV run", "pasted": csv})
        self.assertEqual(response.status_code, 200)

        tournament = Tournament.objects.get(name="CSV run")
        self.assertTrue(tournament.is_fake)
        self.assertEqual(tournament.divisions.get().entrants.count(), 2)

    def test_name_inferred_from_uploaded_filename(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        Player.objects.create(name="Alice", player_number="1", rating=1600)
        self.client.force_login(self.user)
        csv = ("Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
               ",1,Alice,420,Bob,388\n")
        upload = SimpleUploadedFile("nationals-2019.csv", csv.encode(), content_type="text/csv")
        response = self.client.post(self.url, {"name": "", "upload": upload})
        self.assertEqual(response.status_code, 200)
        # Named from the filename stem, not the literal "import".
        tournament = Tournament.objects.get(is_fake=True)
        self.assertIn("nationals-2019", tournament.name)
        self.assertNotIn("import", tournament.name)

    def test_parse_error_creates_nothing(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": "", "pasted": "{ bad json"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not a valid tournament JSON bundle")
        self.assertFalse(Tournament.objects.exists())

    def test_empty_submission_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": "", "pasted": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paste or upload")
        self.assertFalse(Tournament.objects.exists())


@tag("slow")
class TournamentListSplitsWhatIfTests(TestCase):
    """What-if sandboxes list in their own table under the real tournaments.

    Both sandbox kinds set ``is_fake``, so the split keys on the divisions
    instead: a what-if import forces every division ``is_test``, a fake
    tournament deliberately does not.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="lister", password="p")
        self.real = Tournament.objects.create(
            name="Real Open", location="Toronto",
            start_date=date(2026, 5, 1), owner=self.user,
        )
        Division.objects.create(tournament=self.real, name="Division 1")

        self.fake = Tournament.objects.create(
            name="Fake Cup", location="nowhere",
            start_date=date(2026, 5, 2), owner=self.user, is_fake=True,
        )
        Division.objects.create(tournament=self.fake, name="Division 1")

        self.whatif = Tournament.objects.create(
            name="Sandbox Cup", location="What-if sandbox",
            start_date=date(2026, 5, 3), owner=self.user, is_fake=True,
        )
        Division.objects.create(
            tournament=self.whatif, name="Division 1", is_test=True
        )

    def context(self):
        self.client.force_login(self.user)
        return self.client.get(reverse("tournament_list")).context

    def test_only_the_whatif_import_moves_to_the_second_table(self):
        ctx = self.context()
        main = [t.name for t in ctx["tournaments"]]
        whatif = [t.name for t in ctx["whatif_tournaments"]]
        self.assertEqual(whatif, ["Sandbox Cup"])
        # A fake tournament is a sandbox too but stays in the main table: its
        # divisions are not is_test, because it is meant to be fully visible.
        self.assertIn("Fake Cup", main)
        self.assertIn("Real Open", main)
        self.assertNotIn("Sandbox Cup", main)

    def test_the_second_table_appears_below_the_first(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("tournament_list")).content.decode()
        self.assertLess(html.index("Real Open"), html.index("What-if sandboxes"))
        self.assertLess(html.index("What-if sandboxes"), html.index("Sandbox Cup"))

    def test_a_visitor_who_cannot_open_them_is_not_shown_them(self):
        # Their divisions are is_test, which 404s for anyone who cannot edit the
        # tournament, so listing them to a stranger is a row that dies on click.
        self.client.logout()
        html = self.client.get(reverse("tournament_list")).content.decode()
        self.assertNotIn("Sandbox Cup", html)
        self.assertNotIn("What-if sandboxes", html)
        # The real tournaments are still public.
        self.assertIn("Real Open", html)

    def test_the_empty_state_only_shows_when_both_are_empty(self):
        # A division with nothing but sandboxes should not claim there are no
        # tournaments while listing some.
        self.real.delete()
        self.fake.delete()
        self.client.force_login(self.user)
        html = self.client.get(reverse("tournament_list")).content.decode()
        self.assertNotIn("No tournaments yet", html)
        self.assertIn("Sandbox Cup", html)

    def test_the_flag_does_not_cost_a_query_per_row(self):
        """``with_whatif_flag`` annotates, so the split is one EXISTS subquery
        in the list query rather than a lookup per tournament."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        def load():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("tournament_list"))
            return [q["sql"] for q in ctx.captured_queries]

        self.client.force_login(self.user)
        few = load()
        for i in range(20):
            t = Tournament.objects.create(
                name=f"Extra {i}", location="x",
                start_date=date(2026, 4, 1), owner=self.user,
            )
            Division.objects.create(tournament=t, name="Division 1")
        many = load()
        # The only mention of the division table is the EXISTS inside the list
        # query itself, so 20 more tournaments cost no extra queries at all.
        standalone = [q for q in many if q.lstrip().startswith('SELECT "tournaments_division"')]
        self.assertEqual(standalone, [])
        self.assertEqual(len(many), len(few), f"{len(few)} -> {len(many)}")
