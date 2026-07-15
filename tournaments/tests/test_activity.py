"""Phase 3 event-log tests: the Activity page and JSONL export."""

import json

from django.test import TestCase
from django.urls import reverse

from tournaments.commands import create_division
from tournaments.events import describe_event, export_jsonl
from tournaments.models import TournamentEvent
from tournaments.tests.test_models import setUpTournament


class ActivityViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        create_division(self.tournament, self.owner, {"name": "Novice"})

    def test_editor_sees_activity(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_activity", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Created division")

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("tournament_activity", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(
            reverse("tournament_activity", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 302)


class ExportTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        create_division(self.tournament, self.owner, {"name": "Novice"})

    def test_jsonl_has_header_then_events(self):
        text = export_jsonl(self.tournament)
        lines = [json.loads(line) for line in text.strip().split("\n")]
        self.assertEqual(lines[0]["kind"], "header")
        self.assertEqual(lines[0]["tournament"], self.tournament.name)
        # One event line per recorded event, in seq order.
        event_lines = lines[1:]
        self.assertEqual(len(event_lines), self.tournament.events.count())
        self.assertEqual([e["seq"] for e in event_lines], sorted(e["seq"] for e in event_lines))
        self.assertEqual(event_lines[0]["event_type"], "division_created")

    def test_export_view_downloads_jsonl(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse(
                "tournament_event_log_export",
                kwargs={"tournament_slug": self.tournament.slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("division_created", response.content.decode())

    def test_describe_event_is_human_readable(self):
        event = TournamentEvent.objects.filter(event_type="division_created").first()
        self.assertIn("Novice", describe_event(event))
