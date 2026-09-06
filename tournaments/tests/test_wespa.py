"""The WESPA integration: the mirror, the links, and entering somebody from it.

See ``plans/PLAN_WESPA.md``. What these pin down, in the order the plan argues
them, is mostly *restraint* — the pull holds far more back than it writes.
"""

import json
from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.models import (
    Division,
    Entrant,
    Player,
    Tournament,
    WespaPlayer,
    WespaSync,
)
from tournaments.wespa_api import WespaParseError, parse_wespa
from tournaments.wespa_ratings import import_wespa, link_player, unlink_player
from users.models import User


def document(*players):
    """A WESPA document as the endpoint serves one."""
    return json.dumps({"players": list(players)})


def row(wespa_id, name, rating=1500, country="NZL"):
    return {
        "playerid": wespa_id,
        "name": name,
        "country": country,
        "cswrating": rating,
    }


class ParseTests(TestCase):
    def test_the_endpoints_shape_is_read(self):
        rows = parse_wespa(document(row(5, "Adam Logan", 2070, "CAN")))
        self.assertEqual(
            rows,
            [{"wespa_id": 5, "name": "Adam Logan", "country": "CAN", "rating": 2070}],
        )

    def test_a_bare_list_is_accepted(self):
        rows = parse_wespa(json.dumps([row(5, "Adam Logan")]))
        self.assertEqual(len(rows), 1)

    def test_bytes_and_text_are_one_code_path(self):
        """The fetched bytes and an uploaded file must not diverge."""
        text = document(row(5, "Adam Logan"))
        self.assertEqual(parse_wespa(text), parse_wespa(text.encode()))

    def test_a_null_rating_survives_as_none(self):
        rows = parse_wespa(document({"playerid": 5, "name": "A", "cswrating": None}))
        self.assertIsNone(rows[0]["rating"])
        self.assertEqual(rows[0]["country"], "")

    def test_an_unreadable_row_is_fatal_not_skipped(self):
        """Dropping rows quietly would mean a rating silently failing to update."""
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), {"name": "no id"}))
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), row(6, "")))

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaises(WespaParseError):
            parse_wespa(document(row(5, "A"), row(5, "B")))

    def test_junk_is_a_message_not_a_traceback(self):
        for bad in ["not json", "{}", '{"players": []}', "42"]:
            with self.assertRaises(WespaParseError):
                parse_wespa(bad)


class MirrorTests(TestCase):
    def test_the_whole_list_is_kept_even_with_no_players(self):
        """The players Baxter has never seen are the point of the mirror."""
        result = import_wespa(document(row(1, "A"), row(2, "B")))
        self.assertEqual(WespaPlayer.objects.count(), 2)
        self.assertEqual(len(result.added), 2)

    def test_a_pull_never_creates_a_player(self):
        import_wespa(document(row(1, "A"), row(2, "B")))
        self.assertEqual(Player.objects.count(), 0)

    def test_a_second_pull_reports_changes_not_everything(self):
        import_wespa(document(row(1, "A", 1500), row(2, "B", 1400)))
        result = import_wespa(document(row(1, "A", 1550), row(2, "B", 1400)))
        self.assertEqual(result.updated, [1])
        self.assertEqual(result.unchanged, [2])
        self.assertEqual(WespaPlayer.objects.get(wespa_id=1).rating, 1550)

    def test_nothing_is_deleted(self):
        """A row that drops out of the list stays, as the roster pull does."""
        import_wespa(document(row(1, "A"), row(2, "B")))
        import_wespa(document(row(1, "A")))
        self.assertTrue(WespaPlayer.objects.filter(wespa_id=2).exists())


class MatchingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ann = Player.objects.create(
            name="Ann Lee", player_number="0001", rating=1600
        )
        cls.bea = Player.objects.create(
            name="Bea Fox", player_number="0002", rating=0
        )
        cls.twin_a = Player.objects.create(
            name="John Smith", player_number="0010", rating=1500
        )
        cls.twin_b = Player.objects.create(
            name="John Smith", player_number="0011", rating=1300
        )

    def test_a_unique_name_links_and_rates(self):
        result = import_wespa(document(row(7, "Bea Fox", 1450)))
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_id, 7)
        self.assertEqual(self.bea.wespa_rating, 1450)
        self.assertEqual(result.linked, ["Bea Fox"])

    def test_matching_is_case_insensitive(self):
        import_wespa(document(row(7, "bea fox", 1450)))
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_id, 7)

    def test_a_shared_name_links_nobody_and_is_held_back(self):
        """A wrong rating is worse than a missing one."""
        result = import_wespa(document(row(7, "John Smith", 1450)))
        self.twin_a.refresh_from_db()
        self.twin_b.refresh_from_db()
        self.assertIsNone(self.twin_a.wespa_id)
        self.assertIsNone(self.twin_b.wespa_id)
        self.assertIsNone(self.twin_a.wespa_rating)
        self.assertEqual(len(result.pending), 1)
        pending = result.pending[0]
        self.assertEqual(
            {p["player_number"] for p in pending.players}, {"0010", "0011"}
        )
        self.assertEqual([c["wespa_id"] for c in pending.candidates], [7])

    def test_ambiguity_on_the_lists_own_side_is_held_back_too(self):
        """Nothing in the document promises its names are unique."""
        result = import_wespa(document(row(7, "Bea Fox"), row(8, "Bea Fox")))
        self.bea.refresh_from_db()
        self.assertIsNone(self.bea.wespa_id)
        self.assertEqual(len(result.pending), 1)
        self.assertEqual(len(result.pending[0].candidates), 2)

    def test_an_absent_player_is_not_reported(self):
        """Most of the roster has never played a WESPA event; that is not news."""
        result = import_wespa(document(row(7, "Nobody At All")))
        self.assertEqual(result.pending, [])
        self.assertEqual(result.linked, [])

    def test_a_link_outlives_a_name_change(self):
        """The whole reason wespa_id exists."""
        import_wespa(document(row(7, "Bea Fox", 1450)))
        import_wespa(document(row(7, "Bea Fox-Smith", 1500)))
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.name, "Bea Fox")
        self.assertEqual(self.bea.wespa_id, 7)
        self.assertEqual(self.bea.wespa_rating, 1500)

    def test_a_claimed_row_is_not_offered_to_a_namesake(self):
        link_player(self.twin_a, WespaPlayer.objects.create(wespa_id=7, name="X"))
        result = import_wespa(document(row(7, "John Smith", 1450)))
        self.assertEqual(result.pending, [])
        self.twin_b.refresh_from_db()
        self.assertIsNone(self.twin_b.wespa_id)

    def test_a_link_to_a_missing_row_keeps_its_rating(self):
        import_wespa(document(row(7, "Bea Fox", 1450)))
        import_wespa(document(row(9, "Someone Else")))
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_id, 7)
        self.assertEqual(self.bea.wespa_rating, 1450)

    def test_a_coco_rating_still_wins(self):
        """The pull sets a number; it does not change what the cascade picks."""
        import_wespa(document(row(7, "Ann Lee", 1450)))
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.wespa_rating, 1450)
        self.assertEqual(self.ann.effective_rating, (1600, "coco"))

    def test_a_pull_does_not_move_a_pinned_entrant_rating(self):
        """The reason this can stay an unlogged global action."""
        owner = User.objects.create_user(username="td-wespa", password="pw")
        tournament = Tournament.objects.create(
            name="In Progress", location="X",
            start_date=date(2026, 5, 1), owner=owner,
        )
        division = Division.objects.create(tournament=tournament, name="Open")
        entrant = Entrant.enter(division, self.bea, 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

        import_wespa(document(row(7, "Bea Fox", 1450)))

        entrant.refresh_from_db()
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))


class LinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.bea = Player.objects.create(
            name="Bea Fox", player_number="0002", rating=0
        )
        cls.row = WespaPlayer.objects.create(
            wespa_id=7, name="Beatrice Fox", country="NZL", rating=1450
        )

    def test_linking_takes_the_rating(self):
        link_player(self.bea, self.row)
        self.bea.refresh_from_db()
        self.assertEqual((self.bea.wespa_id, self.bea.wespa_rating), (7, 1450))

    def test_a_row_cannot_be_two_people(self):
        other = Player.objects.create(
            name="Other", player_number="0003", rating=0, wespa_id=7
        )
        with self.assertRaises(ValueError):
            link_player(self.bea, self.row)
        self.assertEqual(other.wespa_id, 7)

    def test_unlinking_keeps_the_rating_already_written(self):
        link_player(self.bea, self.row)
        unlink_player(self.bea)
        self.bea.refresh_from_db()
        self.assertIsNone(self.bea.wespa_id)
        self.assertEqual(self.bea.wespa_rating, 1450)


class SyncRecordTests(TestCase):
    """A pull nobody watched has to leave something behind."""

    def test_a_failure_is_recorded_rather_than_raised(self):
        from tournaments import wespa_sync

        record = wespa_sync.run_sync(WespaSync.UPLOAD, b"not json")
        self.assertFalse(record.ok)
        self.assertIn("Not valid JSON", record.error)
        self.assertEqual(WespaSync.objects.count(), 1)

    def test_held_back_names_outlive_the_run(self):
        from tournaments import wespa_sync

        Player.objects.create(name="John Smith", player_number="0010", rating=0)
        Player.objects.create(name="John Smith", player_number="0011", rating=0)
        record = wespa_sync.run_sync(
            WespaSync.SCHEDULED, document(row(7, "John Smith"))
        )
        self.assertTrue(record.ok)
        self.assertEqual(len(record.pending), 1)
        # Read back off the record, as the page does.
        pending = wespa_sync.pending_links()
        self.assertEqual(pending[0].name, "John Smith")
        self.assertEqual(len(pending[0].players), 2)

    def test_a_failed_pull_does_not_hide_the_last_good_ones_work(self):
        from tournaments import wespa_sync

        Player.objects.create(name="John Smith", player_number="0010", rating=0)
        Player.objects.create(name="John Smith", player_number="0011", rating=0)
        wespa_sync.run_sync(WespaSync.SCHEDULED, document(row(7, "John Smith")))
        wespa_sync.run_sync(WespaSync.SCHEDULED, b"broken")
        self.assertEqual(len(wespa_sync.pending_links()), 1)

    def test_a_resolved_name_is_forgotten(self):
        from tournaments import wespa_sync

        Player.objects.create(name="John Smith", player_number="0010", rating=0)
        Player.objects.create(name="John Smith", player_number="0011", rating=0)
        record = wespa_sync.run_sync(
            WespaSync.SCHEDULED, document(row(7, "John Smith"))
        )
        wespa_sync.forget_link(record, "john smith")
        record.refresh_from_db()
        self.assertEqual(record.pending, [])


class WespaPageTests(TestCase):
    """The admin page. Its job is to show state, not just offer a button."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="wespa-admin", password="pw", role=User.Role.ADMIN
        )
        cls.guest = Player.objects.create(
            name="Bea Fox", player_number="T-1", rating=0, is_provisional=True
        )
        cls.row = WespaPlayer.objects.create(
            wespa_id=7, name="Beatrice Fox", country="NZL", rating=1450
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("wespa_import")

    def test_an_unlinked_guest_is_listed(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Bea Fox")
        self.assertContains(response, "Find in WESPA")

    def test_the_search_starts_from_the_players_own_name(self):
        response = self.client.get(self.url, {"link": "T-1"})
        self.assertEqual(response.context["search_query"], "Bea Fox")

    def test_a_hand_search_finds_the_differently_spelled_row(self):
        response = self.client.get(self.url, {"link": "T-1", "q": "Beatrice"})
        self.assertEqual(
            [r.wespa_id for r in response.context["search_results"]], [7]
        )

    def test_linking_by_hand_writes_the_link_and_the_rating(self):
        self.client.post(
            self.url,
            {"action": "link", "player_number": "T-1", "wespa_id": "7"},
        )
        self.guest.refresh_from_db()
        self.assertEqual((self.guest.wespa_id, self.guest.wespa_rating), (7, 1450))

    def test_unlinking_is_reachable_from_the_guest_list(self):
        link_player(self.guest, self.row)
        response = self.client.get(self.url)
        self.assertContains(response, "Unlink")
        self.client.post(
            self.url, {"action": "unlink", "player_number": "T-1"}
        )
        self.guest.refresh_from_db()
        self.assertIsNone(self.guest.wespa_id)

    def test_an_upload_applies_the_document(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            "wespa.json",
            document(row(7, "Beatrice Fox", 1500)).encode(),
            content_type="application/json",
        )
        self.client.post(self.url, {"wespa_file": upload})
        self.assertEqual(WespaPlayer.objects.get(wespa_id=7).rating, 1500)
        self.assertTrue(WespaSync.objects.latest("pk").ok)

    def test_a_bad_upload_is_a_message_not_a_500(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            "wespa.json", b"broken", content_type="application/json"
        )
        response = self.client.post(self.url, {"wespa_file": upload}, follow=True)
        self.assertContains(response, "Not valid JSON")

    def test_a_non_admin_is_kept_out(self):
        self.client.logout()
        other = User.objects.create_user(username="td", password="pw")
        self.client.force_login(other)
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)
