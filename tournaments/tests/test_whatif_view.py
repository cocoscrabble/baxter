"""What-if import view: auth, both formats end-to-end, and error paths."""

import json

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Player, Tournament
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
