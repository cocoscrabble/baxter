"""Phase 3 event-log tests: the Activity page and JSONL export."""

import json

from django.test import TestCase
from django.urls import reverse

from django.db import connection
from django.test.utils import CaptureQueriesContext

from tournaments.commands import create_division
from tournaments.events import (
    describe_event,
    describe_events,
    event_detail,
    export_jsonl,
    record_event,
)
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


class ActivityCostTests(TestCase):
    """The page's cost must not grow with the log.

    Both halves of that were wrong when the detail view was written: every row
    looked its own players up (one query per event), and the filter dropdowns
    were built by pulling the tournament's whole log into memory — which is the
    one thing paging is there to avoid.
    """

    def setUp(self):
        setUpTournament(self)
        self.client.login(username="owner", password="testpass123")

    def url(self):
        return reverse(
            "tournament_activity", kwargs={"tournament_slug": self.tournament.slug}
        )

    def add_events(self, count):
        for i in range(count):
            record_event(self.tournament, "results_saved", {
                "division": self.division.name,
                "added": [{"round": i + 1, "winner": self.player1.player_number,
                           "loser": self.player2.player_number,
                           "winner_score": 450, "loser_score": 400,
                           "winner_started": True}],
                "removed": [], "changed": [],
            }, division=self.division)

    def query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url())
        return len(ctx)

    def test_a_page_of_events_resolves_its_people_in_one_query(self):
        self.add_events(20)
        # select_related as the view does; the point under test is that neither
        # the descriptions nor the details go back for the people row by row.
        events = list(self.tournament.events.select_related("actor", "division"))
        with self.assertNumQueries(1):
            described = describe_events(events)
        _description, detail = described[0]
        self.assertIn("Alice", detail["lines"][0][2])

    def test_the_page_costs_the_same_with_ten_times_the_log(self):
        self.add_events(20)
        self.client.get(self.url())  # warm any per-process caches
        small = self.query_count()
        self.add_events(200)
        self.assertEqual(self.query_count(), small)

    def test_no_query_reads_more_of_the_log_than_the_page_shows(self):
        """The query count alone does not catch this one.

        Building the filter lists in Python is *one* query too — it just
        happens to be a query that returns every event in the tournament. So
        assert what actually matters: nothing selects whole event rows beyond
        the page, and the columns the filters need come back already distinct.
        """
        self.add_events(120)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(self.url())
        self.assertEqual(len(response.context["events"]), 100)
        filter_queries = [
            q["sql"] for q in ctx.captured_queries
            if "tournaments_tournamentevent" in q["sql"] and "COUNT(*)" not in q["sql"]
        ]
        page_queries = [q for q in filter_queries if "LIMIT" in q]
        self.assertEqual(len(page_queries), 1, "one query should fetch the page")
        for sql in filter_queries:
            if sql in page_queries:
                continue
            self.assertIn("DISTINCT", sql, f"reads the whole log: {sql[:120]}")


class EventDetailTests(TestCase):
    """What an event did, in one compact line per row.

    The page is for a director skimming for the one thing that happened, so a
    row is a line in the vocabulary they use — "R3  Femi Awowade (500) – Dean
    Saldanha (442)" — not a record laid out in fields, and never JSON. The
    downloaded log is the machine-readable copy.
    """

    def setUp(self):
        setUpTournament(self)

    def detail(self, event_type, payload, summary=""):
        from tournaments.events import describe_event

        event = record_event(
            self.tournament, event_type, payload, division=self.division
        )
        return event_detail(event, summary=summary or describe_event(event))

    def texts(self, detail):
        return [text for _kind, _mark, text in detail["lines"]]

    def marks(self, detail):
        return [kind for kind, _mark, _text in detail["lines"]]

    @property
    def alice(self):
        # The number the model stored, not the one the fixture typed: Player
        # normalises it, and a payload holds what was stored.
        return self.player1.player_number

    @property
    def bob(self):
        return self.player2.player_number

    def result(self, **over):
        row = {"round": 3, "winner": self.alice, "loser": self.bob,
               "winner_score": 500, "loser_score": 442, "winner_started": True}
        return {**row, **over}

    def results_saved(self, **payload):
        return self.detail("results_saved", {
            "division": self.division.name,
            "added": [], "removed": [], "changed": [], **payload,
        })

    # -- results ---------------------------------------------------------

    def test_a_result_reads_as_the_board_had_it(self):
        # winner_started, so the winner is on the left and carries the tick.
        detail = self.results_saved(added=[self.result()])
        self.assertEqual(self.texts(detail), ["R3  Alice (500) ✓ – Bob (442)"])
        self.assertEqual(self.marks(detail), ["added"])

    def test_the_player_who_went_first_is_on_the_left(self):
        detail = self.results_saved(added=[self.result(winner_started=False)])
        # Bob went first and lost; the tick says who won all the same.
        self.assertEqual(self.texts(detail), ["R3  Bob (442) – Alice (500) ✓"])

    def test_a_bye_puts_its_real_player_first(self):
        # No board, so board order means nothing; the real player reads first,
        # the way the pairing is stored.
        detail = self.results_saved(added=[self.result(
            loser="BYE", winner_score=50, loser_score=0, winner_started=False,
        )])
        self.assertEqual(self.texts(detail), ["R3  Alice (50) ✓ – BYE (0)"])

    def test_a_forfeit_reads_the_same_way_round(self):
        detail = self.results_saved(added=[self.result(
            winner="BYE", loser=self.alice, winner_score=50, loser_score=0,
            winner_started=True,
        )])
        self.assertEqual(self.texts(detail), ["R3  Alice (0) – BYE (50) ✓"])

    def test_a_corrected_score_moves_inside_the_line(self):
        detail = self.results_saved(changed=[{
            "from": self.result(),
            "to": self.result(winner_score=502, loser_score=440),
        }])
        self.assertEqual(
            self.texts(detail), ["R3  Alice (500→502) ✓ – Bob (442→440)"]
        )
        self.assertEqual(self.marks(detail), ["changed"])

    def test_only_the_score_that_moved_gets_an_arrow(self):
        detail = self.results_saved(changed=[{
            "from": self.result(), "to": self.result(winner_score=502),
        }])
        self.assertEqual(self.texts(detail), ["R3  Alice (500→502) ✓ – Bob (442)"])

    def test_a_corrected_winner_says_so_rather_than_faking_an_arrow(self):
        # The scores moved between two people, so an arrow on either would be a
        # lie about what happened.
        detail = self.results_saved(changed=[{
            "from": self.result(),
            "to": self.result(winner=self.bob, loser=self.alice,
                              winner_score=442, loser_score=500),
        }])
        self.assertEqual(
            self.texts(detail),
            ["R3  Bob (442) ✓ – Alice (500) · winner was Alice"],
        )

    def test_a_changed_start_is_named(self):
        detail = self.results_saved(changed=[{
            "from": self.result(), "to": self.result(winner_started=False),
        }])
        self.assertEqual(
            self.texts(detail), ["R3  Bob (442) – Alice (500) ✓ · Bob started"]
        )

    def test_a_removed_result_is_the_same_line_marked_out(self):
        detail = self.results_saved(removed=[self.result()])
        self.assertEqual(self.texts(detail), ["R3  Alice (500) ✓ – Bob (442)"])
        self.assertEqual(self.marks(detail), ["removed"])

    # -- entrants --------------------------------------------------------

    def test_an_entrant_reads_as_their_registration(self):
        detail = self.detail("entrants_saved", {
            "division": self.division.name, "removed": [], "changed": [],
            "added": [{"number": 3, "player": self.alice, "name": "Alice",
                       "entrant_rating": 1804, "rating_source": "coco",
                       "paid": True, "dropped": False, "tentative": False,
                       "playing_up": False}],
        })
        self.assertEqual(self.texts(detail), ["Alice  #3 · 1804 coco · paid"])

    def test_a_flag_that_went_on_is_just_its_word(self):
        before = {"number": 3, "player": self.alice, "name": "Alice",
                  "paid": False, "entrant_rating": 1128}
        detail = self.detail("entrants_saved", {
            "division": self.division.name, "added": [], "removed": [],
            "changed": [{"from": before,
                         "to": {**before, "paid": True, "entrant_rating": 1200}}],
        })
        self.assertEqual(self.texts(detail), ["Alice  rating 1128 → 1200 · paid"])

    def test_a_flag_that_went_off_is_negated(self):
        before = {"number": 3, "player": self.alice, "name": "Alice", "paid": True}
        detail = self.detail("entrants_saved", {
            "division": self.division.name, "added": [], "removed": [],
            "changed": [{"from": before, "to": {**before, "paid": False}}],
        })
        self.assertEqual(self.texts(detail), ["Alice  not paid"])

    # -- command payloads ------------------------------------------------

    def test_a_payload_the_summary_already_said_unfolds_to_nothing(self):
        # "Published round 2 in Division 1" has nothing left to show.
        detail = self.detail("round_published", {"division": "Open", "round": 2})
        self.assertEqual(detail, {})

    def test_a_single_result_reads_like_a_result_not_like_a_form(self):
        # The commonest thing a director does, in the same shape the grid uses.
        detail = self.detail("result_added", {
            "division": self.division.name, "round": 9,
            "first_player": self.alice, "second_player": self.bob,
            "winner_player": self.bob, "winner_score": 500, "loser_score": 442,
        })
        # first_player is Alice, so the board had her on the left.
        self.assertEqual(self.texts(detail), ["R9  Alice (442) – Bob (500) ✓"])

    def test_any_other_command_payload_is_one_compact_line(self):
        detail = self.detail("entrant_updated", {
            "division": self.division.name, "player": self.alice,
            "paid": True, "payment_note": "cash",
        }, summary="Updated a registration")
        line = self.texts(detail)[0]
        self.assertIn("player Alice", line)
        self.assertIn("paid yes", line)
        self.assertNotIn("{", line)
        self.assertNotIn('"', line)

    def test_a_person_the_summary_already_named_is_not_repeated(self):
        detail = self.detail("game_forfeited", {
            "division": self.division.name, "player": self.alice, "round": 4,
        })
        # "Alice forfeited round 4 in Division 1" — both facts are in the line
        # above, so there is nothing left to unfold.
        self.assertEqual(detail, {})

    def test_people_nested_inside_a_payload_are_named_too(self):
        # entrant_ratings_refreshed keeps its people inside a list of entrants,
        # and entrants_reseeded inside a list of pairs. A per-key rule would
        # miss both.
        detail = self.detail("entrant_ratings_refreshed", {
            "division": self.division.name,
            "entrants": [{"player": self.alice, "rating": 1605,
                          "rating_source": "wespa"}],
        })
        line = self.texts(detail)[0]
        self.assertIn("player=Alice", line)
        self.assertIn("rating=1605", line)

    def test_a_seeding_reads_as_a_run_not_a_nest_of_brackets(self):
        detail = self.detail("entrants_reseeded", {
            "division": self.division.name,
            "seeding": [[self.alice, 1], [self.bob, 2]],
        })
        self.assertEqual(self.texts(detail), ["seeding Alice, 1, Bob, 2"])

    def test_a_value_that_is_nobody_is_left_alone(self):
        detail = self.detail("division_renamed", {
            "old_name": "Open", "new_name": "Division 1",
        }, summary="Renamed a division")
        line = self.texts(detail)[0]
        self.assertIn("old name Open", line)
        self.assertIn("new name Division 1", line)

    def test_a_long_payload_is_cut_rather_than_run_off_the_row(self):
        from tournaments.events import MAX_LINE_CHARS

        detail = self.detail("division_imported", {
            "division": self.division.name,
            "note": " ".join(["word"] * 200),
        })
        line = self.texts(detail)[0]
        self.assertLessEqual(len(line), MAX_LINE_CHARS + 2)
        self.assertTrue(line.endswith("…"))

    def test_a_snapshot_says_what_it_holds_and_points_at_the_file(self):
        detail = self.detail("state_snapshot", {
            "divisions": [{"entrants": [{"pad": "x" * 5000}], "results": []}],
        })
        self.assertIn("1 division(s), 1 entrant(s)", detail["note"])
        self.assertIn("Download the log", detail["note"])
        self.assertNotIn("lines", detail)

    # -- grid saves from before deltas ------------------------------------

    def test_a_whole_collection_payload_reads_as_its_rows(self):
        detail = self.detail("results_saved", {
            "division": self.division.name, "rows": [self.result()],
        })
        self.assertEqual(self.texts(detail), ["R3  Alice (500) ✓ – Bob (442)"])
        self.assertEqual(self.marks(detail), [""])

    # -- the cap -----------------------------------------------------------

    def test_a_bulk_save_is_capped_and_says_how_much_it_held_back(self):
        from tournaments.events import MAX_DETAIL_ROWS

        count = MAX_DETAIL_ROWS + 150
        detail = self.detail("entrants_saved", {
            "division": self.division.name,
            "removed": [], "changed": [],
            "added": [{"number": i, "player": self.alice, "name": f"P{i}"}
                      for i in range(count)],
        })
        self.assertEqual(len(detail["lines"]), MAX_DETAIL_ROWS)
        self.assertEqual(detail["more"], 150)

    def test_an_ordinary_save_holds_nothing_back(self):
        detail = self.results_saved(added=[self.result()])
        self.assertEqual(detail["more"], 0)

    def test_the_page_shows_the_line(self):
        self.results_saved(changed=[{
            "from": self.result(), "to": self.result(winner_score=502)
        }])
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_activity",
                    kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertContains(response, "R3  Alice (500→502) ✓ – Bob (442)")
        self.assertNotContains(response, "&quot;winner&quot;")


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
