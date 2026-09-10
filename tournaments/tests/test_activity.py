"""Phase 3 event-log tests: the Activity page and JSONL export."""

import json

from django.test import TestCase
from django.urls import reverse

from tournaments.commands import create_division
from tournaments.events import describe_event, event_detail, export_jsonl, record_event
from tournaments.models import TournamentEvent
from tournaments.tests.test_models import setUpTournament
from users.models import User


class ActivityViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        create_division(self.tournament, self.owner, {"name": "Novice"})

    def url(self):
        return reverse(
            "tournament_activity", kwargs={"tournament_slug": self.tournament.slug}
        )

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

    def test_a_supervisor_sees_a_tournament_they_do_not_run(self):
        # Asked what happened at somebody else's event, a supervisor has to be
        # able to look; the log is the answer.
        User.objects.create_user(
            username="sup", password="testpass123", role=User.Role.SUPERVISOR
        )
        self.client.login(username="sup", password="testpass123")
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)

    def test_a_plain_director_still_sees_only_their_own(self):
        # Director is the default role every account holds, so it cannot be what
        # opens a stranger's log.
        self.assertEqual(
            User.objects.get(username="other").role, User.Role.DIRECTOR
        )
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_the_export_has_the_same_readers(self):
        User.objects.create_user(
            username="sup2", password="testpass123", role=User.Role.SUPERVISOR
        )
        url = reverse(
            "tournament_event_log_export",
            kwargs={"tournament_slug": self.tournament.slug},
        )
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.login(username="sup2", password="testpass123")
        self.assertEqual(self.client.get(url).status_code, 200)


class ActivityFilterTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        create_division(self.tournament, self.owner, {"name": "Novice"})
        self.client.login(username="owner", password="testpass123")

    def url(self, **params):
        base = reverse(
            "tournament_activity", kwargs={"tournament_slug": self.tournament.slug}
        )
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}?{query}" if query else base

    def seqs(self, response):
        return [e.seq for e, _description, _detail in response.context["events"]]

    def test_filtering_by_division_narrows_the_log(self):
        novice = self.tournament.divisions.get(name="Novice")
        record_event(
            self.tournament, "round_published", {"division": "Novice", "round": 1},
            division=novice,
        )
        response = self.client.get(self.url(division="Novice"))
        types = {e.event_type for e, _d, _x in response.context["events"]}
        self.assertEqual(types, {"division_created", "round_published"})
        self.assertEqual(
            {e.division.name for e, _d, _x in response.context["events"]}, {"Novice"}
        )

    def test_filtering_by_type_narrows_the_log(self):
        record_event(self.tournament, "round_published", {"round": 1})
        response = self.client.get(self.url(type="round_published"))
        self.assertEqual(
            {e.event_type for e, _d, _x in response.context["events"]},
            {"round_published"},
        )

    def test_the_filters_offer_only_what_the_log_holds(self):
        response = self.client.get(self.url())
        # Not the whole catalog: this tournament has published nothing.
        self.assertNotIn("round_published", response.context["event_types"])
        self.assertIn("division_created", response.context["event_types"])

    def test_paging_keeps_the_filter(self):
        for i in range(3):
            record_event(self.tournament, "round_published", {"round": i + 1})
        with self.settings():
            from tournaments.views import TournamentActivityView

            original, TournamentActivityView.per_page = (
                TournamentActivityView.per_page, 2
            )
            try:
                page1 = self.client.get(self.url(type="round_published"))
                page2 = self.client.get(self.url(type="round_published", page=2))
            finally:
                TournamentActivityView.per_page = original
        self.assertEqual(len(self.seqs(page1)), 2)
        self.assertEqual(len(self.seqs(page2)), 1)
        # Newest first, and the two pages do not overlap.
        self.assertEqual(sorted(self.seqs(page1) + self.seqs(page2), reverse=True),
                         self.seqs(page1) + self.seqs(page2))
        self.assertContains(page1, "type=round_published")


class EventDetailTests(TestCase):
    """What an event did, rendered in the grid's own vocabulary."""

    def setUp(self):
        setUpTournament(self)

    def detail(self, event_type, payload):
        return event_detail(
            record_event(self.tournament, event_type, payload, division=self.division)
        )

    @property
    def alice(self):
        # The number the model stored, not the one the fixture typed: Player
        # normalises it, and a payload holds what was stored.
        return self.player1.player_number

    @property
    def bob(self):
        return self.player2.player_number

    def test_an_edited_result_names_the_players_and_the_fields_that_moved(self):
        detail = self.detail("results_saved", {
            "division": self.division.name,
            "added": [], "removed": [],
            "changed": [{
                "from": {"round": 3, "winner": self.alice, "loser": self.bob,
                         "winner_score": 500, "loser_score": 442,
                         "winner_started": True},
                "to": {"round": 3, "winner": self.alice, "loser": self.bob,
                       "winner_score": 502, "loser_score": 440,
                       "winner_started": True},
            }],
        })
        row = detail["changed"][0]
        self.assertEqual(row["label"], "Round 3: Alice beat Bob")
        self.assertEqual(
            row["changes"],
            [("winner_score", "500", "502"), ("loser_score", "442", "440")],
        )

    def test_a_corrected_winner_shows_as_the_swap_it_is(self):
        detail = self.detail("results_saved", {
            "division": self.division.name,
            "added": [], "removed": [],
            "changed": [{
                "from": {"round": 1, "winner": self.alice, "loser": self.bob,
                         "winner_score": 500, "loser_score": 442,
                         "winner_started": True},
                "to": {"round": 1, "winner": self.bob, "loser": self.alice,
                       "winner_score": 500, "loser_score": 442,
                       "winner_started": True},
            }],
        })
        row = detail["changed"][0]
        self.assertEqual(row["label"], "Round 1: Bob beat Alice")
        self.assertIn(("winner", "Alice", "Bob"), row["changes"])
        self.assertIn(("loser", "Bob", "Alice"), row["changes"])

    def test_an_added_entrant_reads_as_values_under_their_name(self):
        detail = self.detail("entrants_saved", {
            "division": self.division.name,
            "removed": [], "changed": [],
            "added": [{"number": 1, "player": self.alice, "name": "Alice",
                       "rating": 1600, "entrant_rating": 1600,
                       "rating_source": "coco", "dropped": False,
                       "tentative": False, "paid": True, "playing_up": False}],
        })
        row = detail["added"][0]
        self.assertEqual(row["label"], "Alice")
        fields = dict(row["fields"])
        # Booleans read as what the checkbox says, not as True/False.
        self.assertEqual(fields["paid"], "yes")
        self.assertEqual(fields["dropped"], "no")
        # The name is the label, so it is not repeated as a field.
        self.assertNotIn("name", fields)
        self.assertNotIn("player", fields)

    def test_a_removed_row_is_named_and_left_at_that(self):
        detail = self.detail("entrants_saved", {
            "division": self.division.name,
            "added": [], "changed": [],
            "removed": [{"number": 2, "player": self.bob, "name": "Bob"}],
        })
        self.assertEqual(detail["removed"], [{"label": "Bob"}])

    def test_a_command_payload_is_shown_as_recorded(self):
        detail = self.detail("round_published", {"division": "Open", "round": 2})
        self.assertIn('"round": 2', detail["payload"])

    def test_a_whole_collection_payload_is_shown_as_recorded(self):
        # A grid save from before deltas: there is no diff to render.
        detail = self.detail("entrants_saved", {
            "division": self.division.name,
            "rows": [{"number": 1, "player": self.alice}],
        })
        self.assertIn('"rows"', detail["payload"])

    def test_a_huge_payload_is_truncated_rather_than_pasted_in(self):
        from tournaments.events import MAX_PAYLOAD_CHARS

        detail = self.detail(
            "state_snapshot", {"divisions": [{"pad": "x" * MAX_PAYLOAD_CHARS}]}
        )
        self.assertIn("truncated", detail["payload"])
        self.assertLess(len(detail["payload"]), MAX_PAYLOAD_CHARS + 200)

    def test_the_page_shows_the_detail(self):
        self.detail("results_saved", {
            "division": self.division.name,
            "added": [], "removed": [],
            "changed": [{
                "from": {"round": 3, "winner": self.alice, "loser": self.bob,
                         "winner_score": 500, "loser_score": 442,
                         "winner_started": True},
                "to": {"round": 3, "winner": self.alice, "loser": self.bob,
                       "winner_score": 502, "loser_score": 442,
                       "winner_started": True},
            }],
        })
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_activity",
                    kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertContains(response, "Round 3: Alice beat Bob")
        self.assertContains(response, "winner_score")


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
