import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from tournaments.models import Player
from tournaments.player_sync import export_players, import_players
from users.models import User


def make_player(number, name, rating=1000):
    """Create a player. Note the stored number comes back canonical (7 -> 0007)."""
    return Player.objects.create(player_number=number, name=name, rating=rating)


class ImportPlayersTests(TestCase):
    def test_inserts_new_players(self):
        result, errors = import_players([
            {"player_number": "1", "name": "Alice", "rating": 1500},
            {"player_number": "2", "name": "Bob", "rating": 1200},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(result, {"total": 2, "added": 2, "updated": 0, "unchanged": 0})
        self.assertEqual(Player.objects.count(), 2)
        self.assertEqual(Player.objects.get(player_number="0001").name, "Alice")

    def test_updates_existing_by_player_number(self):
        make_player("7", "Old Name", 1000)
        result, errors = import_players([
            {"player_number": "7", "name": "New Name", "rating": 1800},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["added"], 0)
        player = Player.objects.get(player_number="0007")
        self.assertEqual(player.name, "New Name")
        self.assertEqual(player.rating, 1800)

    def test_unchanged_rows_are_counted_not_rewritten(self):
        make_player("3", "Carol", 1100)
        result, _ = import_players([
            {"player_number": "3", "name": "Carol", "rating": 1100},
        ])
        self.assertEqual(result, {"total": 1, "added": 0, "updated": 0, "unchanged": 1})

    def test_never_deletes_players_absent_from_upload(self):
        make_player("99", "Keeper", 1000)
        import_players([{"player_number": "1", "name": "Alice", "rating": 1500}])
        self.assertTrue(Player.objects.filter(player_number="0099").exists())

    def test_preserves_primary_keys_of_existing_players(self):
        existing = make_player("5", "Dave", 1000)
        import_players([{"player_number": "5", "name": "Dave Updated", "rating": 1300}])
        self.assertEqual(Player.objects.get(player_number="0005").pk, existing.pk)

    def test_bare_upload_updates_a_padded_row(self):
        """A bare number in the upload is the same person as the padded row.

        The registry writes numbers bare (7) and Baxter stores them canonical
        (0007). Matching raw strings would miss the existing row and insert a
        second copy of the same person -- silently, and permanently.
        """
        existing = make_player("7", "Old Name", 1000)
        self.assertEqual(existing.player_number, "0007")
        result, errors = import_players([
            {"player_number": "7", "name": "New Name", "rating": 1800},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(result, {"total": 1, "added": 0, "updated": 1, "unchanged": 0})
        self.assertEqual(Player.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "New Name")

    def test_over_padded_upload_is_the_same_person(self):
        existing = make_player("7", "Old Name", 1000)
        import_players([{"player_number": "00007", "name": "New Name", "rating": 1800}])
        self.assertEqual(Player.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "New Name")

    def test_deduplicates_within_upload_last_wins(self):
        result, _ = import_players([
            {"player_number": "1", "name": "First", "rating": 1000},
            {"player_number": "1", "name": "Second", "rating": 2000},
        ])
        self.assertEqual(result["added"], 1)
        self.assertEqual(Player.objects.get(player_number="0001").name, "Second")

    def test_accepts_json_string_and_bytes(self):
        payload = [{"player_number": "1", "name": "Alice", "rating": 1500}]
        result, _ = import_players(json.dumps(payload))
        self.assertEqual(result["added"], 1)
        result, _ = import_players(json.dumps(payload).encode())
        self.assertEqual(result["updated"] + result["unchanged"], 1)

    def test_rejects_invalid_json(self):
        result, errors = import_players("{not json")
        self.assertIsNone(result)
        self.assertEqual(errors, ["File is not valid JSON."])

    def test_rejects_non_list(self):
        result, errors = import_players({"player_number": "1"})
        self.assertIsNone(result)
        self.assertIn("Expected a JSON list", errors[0])

    def test_reports_missing_fields_and_writes_nothing(self):
        result, errors = import_players([
            {"name": "No Number", "rating": 1000},
            {"player_number": "2", "rating": 1000},
        ])
        self.assertIsNone(result)
        self.assertEqual(len(errors), 2)
        self.assertEqual(Player.objects.count(), 0)

    def test_rejects_non_numeric_rating(self):
        result, errors = import_players([
            {"player_number": "1", "name": "Alice", "rating": "abc"},
        ])
        self.assertIsNone(result)
        self.assertIn("rating", errors[0])

    def test_export_round_trips_through_import(self):
        make_player("A1", "Zoe", 1234)
        make_player("A2", "Yan", 999)
        exported = export_players()
        Player.objects.all().delete()
        result, errors = import_players(exported)
        self.assertEqual(errors, [])
        self.assertEqual(result["added"], 2)
        self.assertEqual(Player.objects.get(player_number="A1").name, "Zoe")


class PlayerImportViewAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.url = reverse("player_import")
        cls.director = User.objects.create_user(username="director", password="pw")
        cls.admin_role = User.objects.create_user(
            username="adminrole", password="pw", role=User.Role.ADMIN
        )
        cls.superuser = User.objects.create_superuser(username="super", password="pw")

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_non_admin_forbidden(self):
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_admin_role_allowed(self):
        self.client.force_login(self.admin_role)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_superuser_allowed(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(self.url).status_code, 200)


class PlayerImportViewUploadTests(TestCase):
    def setUp(self):
        self.url = reverse("player_import")
        self.superuser = User.objects.create_superuser(username="super", password="pw")
        self.client.force_login(self.superuser)

    def _upload(self, payload):
        content = json.dumps(payload).encode()
        return self.client.post(
            self.url,
            {"players_file": SimpleUploadedFile("players.json", content)},
            follow=True,
        )

    def test_upload_upserts_players(self):
        make_player("9", "Existing", 1000)
        response = self._upload([
            {"player_number": "9", "name": "Existing Renamed", "rating": 1700},
            {"player_number": "10", "name": "Brand New", "rating": 1400},
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Player.objects.get(player_number="0009").name, "Existing Renamed")
        self.assertTrue(Player.objects.filter(player_number="0010").exists())

    def test_missing_file_is_reported(self):
        response = self.client.post(self.url, {}, follow=True)
        self.assertContains(response, "No file uploaded")


class WespaRefreshTests(TestCase):
    """WESPA ratings refresh the roster without touching anything in progress.

    Matching follows the entrant CSV import's rule: a number resolves exactly, a
    unique name resolves, and a shared name resolves *nothing* — WESPA cannot
    say which "John Smith" it means, and a wrong rating is worse than a missing
    one.
    """

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

    def _refresh(self, text):
        from tournaments.wespa_ratings import parse_wespa_csv, refresh_wespa_ratings

        rows, errors = parse_wespa_csv(text)
        self.assertEqual(errors, [])
        return refresh_wespa_ratings(rows)

    def test_a_new_rating_is_set(self):
        result = self._refresh("Bea Fox,1450\n")
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_rating, 1450)
        self.assertEqual(result.updated, ["Bea Fox"])

    def test_an_existing_rating_is_updated(self):
        self.bea.wespa_rating = 1400
        self.bea.save(update_fields=["wespa_rating"])
        self._refresh("Bea Fox,1450\n")
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_rating, 1450)

    def test_an_unchanged_rating_is_reported_as_such(self):
        self.bea.wespa_rating = 1450
        self.bea.save(update_fields=["wespa_rating"])
        result = self._refresh("Bea Fox,1450\n")
        self.assertEqual(result.unchanged, ["Bea Fox"])
        self.assertEqual(result.updated, [])

    def test_an_unknown_name_is_a_no_op_and_reported(self):
        result = self._refresh("Nobody At All,1450\n")
        self.assertEqual(result.unmatched, ["Nobody At All"])
        self.assertEqual(result.updated, [])

    def test_an_ambiguous_name_updates_nobody(self):
        result = self._refresh("John Smith,1450\n")
        self.twin_a.refresh_from_db()
        self.twin_b.refresh_from_db()
        self.assertIsNone(self.twin_a.wespa_rating)
        self.assertIsNone(self.twin_b.wespa_rating)
        self.assertEqual(len(result.ambiguous), 1)
        self.assertIn("#0010", result.ambiguous[0])
        self.assertIn("#0011", result.ambiguous[0])

    def test_a_number_resolves_one_of_two_same_named_players(self):
        result = self._refresh("0011,John Smith,1450\n")
        self.twin_a.refresh_from_db()
        self.twin_b.refresh_from_db()
        self.assertIsNone(self.twin_a.wespa_rating)
        self.assertEqual(self.twin_b.wespa_rating, 1450)
        self.assertEqual(result.ambiguous, [])

    def test_a_number_is_canonicalized_like_everywhere_else(self):
        self._refresh("11,John Smith,1450\n")
        self.twin_b.refresh_from_db()
        self.assertEqual(self.twin_b.wespa_rating, 1450)

    def test_an_unknown_number_is_reported_not_guessed_by_name(self):
        result = self._refresh("9999,Ann Lee,1450\n")
        self.ann.refresh_from_db()
        self.assertIsNone(self.ann.wespa_rating)
        self.assertEqual(result.unmatched, ["9999"])

    def test_a_heading_row_is_skipped(self):
        self._refresh("player_number,name,wespa_rating\n0002,Bea Fox,1450\n")
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_rating, 1450)

    def test_one_bad_row_does_not_block_the_others(self):
        result = self._refresh("Nobody,1400\nBea Fox,1450\n")
        self.bea.refresh_from_db()
        self.assertEqual(self.bea.wespa_rating, 1450)
        self.assertEqual(result.unmatched, ["Nobody"])

    def test_a_coco_rating_still_wins_after_a_wespa_refresh(self):
        """The refresh sets a number; it does not change what the cascade picks."""
        self._refresh("Ann Lee,1450\n")
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.wespa_rating, 1450)
        self.assertEqual(self.ann.effective_rating, (1600, "coco"))

    def test_a_refresh_does_not_move_a_pinned_entrant_rating(self):
        """The reason this can stay an unlogged global action."""
        from datetime import date

        from tournaments.models import Division, Entrant, Tournament

        owner = User.objects.create_user(username="td-wespa", password="pw")
        tournament = Tournament.objects.create(
            name="In Progress", location="X",
            start_date=date(2026, 5, 1), owner=owner,
        )
        division = Division.objects.create(tournament=tournament, name="Open")
        entrant = Entrant.enter(division, self.bea, 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

        self._refresh("Bea Fox,1450\n")

        entrant.refresh_from_db()
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))


class WespaParseTests(TestCase):
    def _parse(self, text):
        from tournaments.wespa_ratings import parse_wespa_csv

        return parse_wespa_csv(text)

    def test_two_and_three_column_forms(self):
        rows, errors = self._parse("Ann,1400\n0002,Bea,1500\n")
        self.assertEqual(errors, [])
        self.assertEqual(rows, [("", "Ann", 1400), ("0002", "Bea", 1500)])

    def test_an_empty_file_is_an_error(self):
        rows, errors = self._parse("")
        self.assertEqual(rows, [])
        self.assertTrue(errors)

    def test_a_bad_column_count_is_reported(self):
        rows, errors = self._parse("a,b,c,d\n")
        self.assertEqual(rows, [])
        self.assertTrue(any("2 or 3 columns" in e for e in errors))

    def test_a_non_numeric_rating_is_reported(self):
        rows, errors = self._parse("Ann,not-a-number\n")
        self.assertEqual(rows, [])
        self.assertTrue(any("invalid rating" in e for e in errors))
