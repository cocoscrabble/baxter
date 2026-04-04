from datetime import date

from django.test import TestCase

from tournaments.import_entrants import import_entrants, parse_csv, resolve_players
from tournaments.models import Division, Entrant, Player, Tournament
from users.models import User


class ParseCsvTests(TestCase):
    def test_one_column_format(self):
        parsed, errors = parse_csv("Alice\nBob\n")
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [("Alice", 0), ("Bob", 0)])

    def test_two_column_format(self):
        parsed, errors = parse_csv("Alice,1600\nBob,1500\n")
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [("Alice", 1600), ("Bob", 1500)])

    def test_empty_rating_defaults_to_zero(self):
        parsed, errors = parse_csv("Alice,\n")
        self.assertEqual(errors, [])
        self.assertEqual(parsed, [("Alice", 0)])

    def test_empty_file(self):
        parsed, errors = parse_csv("")
        self.assertEqual(parsed, [])
        self.assertIn("File is empty.", errors)

    def test_wrong_column_count(self):
        parsed, errors = parse_csv("a,b,c\n")
        self.assertEqual(parsed, [])
        self.assertTrue(any("expected 1 or 2 columns" in e for e in errors))

    def test_missing_name(self):
        parsed, errors = parse_csv(",1600\n")
        self.assertEqual(parsed, [])
        self.assertTrue(any("name is required" in e for e in errors))

    def test_duplicate_names_rejected(self):
        parsed, errors = parse_csv("Alice,1600\nAlice,1500\n")
        self.assertEqual(len(parsed), 1)
        self.assertTrue(any("duplicate" in e.lower() for e in errors))

    def test_duplicate_names_case_insensitive(self):
        parsed, errors = parse_csv("Alice,1600\nalice,1500\n")
        self.assertTrue(any("duplicate" in e.lower() for e in errors))

    def test_invalid_rating(self):
        parsed, errors = parse_csv("Alice,abc\n")
        self.assertEqual(parsed, [])
        self.assertTrue(any("invalid rating" in e for e in errors))

    def test_blank_rows_skipped(self):
        parsed, errors = parse_csv("Alice,1600\n\n\nBob,1500\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 2)


class ResolvePlayersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = Player.objects.create(name="Alice", player_number="001", rating=1600)

    def test_matches_existing_by_name(self):
        players, result, errors = resolve_players(
            [("Alice", 1600)], existing_entrant_names=set()
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].pk, self.alice.pk)
        self.assertEqual(result.matched, ["Alice"])

    def test_skips_existing_entrant(self):
        players, result, errors = resolve_players(
            [("Alice", 1600)], existing_entrant_names={"alice"}
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(players), 0)
        self.assertEqual(result.skipped, ["Alice"])

    def test_creates_new_player(self):
        players, result, errors = resolve_players(
            [("Charlie", 1400)], existing_entrant_names=set()
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].name, "Charlie")
        self.assertEqual(len(result.created), 1)
        self.assertTrue(Player.objects.filter(name="Charlie").exists())

    def test_existing_player_rating_preserved(self):
        """CSV rating is ignored for existing players."""
        players, result, errors = resolve_players(
            [("Alice", 9999)], existing_entrant_names=set()
        )
        self.assertEqual(errors, [])
        self.assertEqual(players[0].rating, 1600)  # original, not 9999


class ImportEntrantsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test", location="Test", start_date=date(2026, 3, 15), owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)
        cls.alice = Player.objects.create(name="Alice", player_number="001", rating=1600)
        Entrant.objects.create(division=cls.division, player=cls.alice, number=1)

    def test_full_import_new_players(self):
        result, errors = import_entrants(self.division, "Charlie,1400\nDave,1300\n")
        self.assertEqual(errors, [])
        self.assertEqual(result.added, 2)
        self.assertEqual(len(result.created), 2)
        self.assertEqual(self.division.entrants.count(), 3)

    def test_skips_existing_entrant(self):
        result, errors = import_entrants(self.division, "Alice,1600\n")
        self.assertEqual(errors, [])
        self.assertEqual(result.added, 0)
        self.assertEqual(result.skipped, ["Alice"])

    def test_entrant_numbers_append(self):
        result, errors = import_entrants(self.division, "Eve,1200\n")
        self.assertEqual(errors, [])
        entrant = self.division.entrants.get(player__name="Eve")
        self.assertEqual(entrant.number, 2)

    def test_parse_errors_prevent_changes(self):
        result, errors = import_entrants(self.division, "Alice,1600\nAlice,1500\n")
        self.assertTrue(len(errors) > 0)
        self.assertIsNone(result)

    def test_name_only_import(self):
        result, errors = import_entrants(self.division, "Frank\n")
        self.assertEqual(errors, [])
        self.assertEqual(result.added, 1)
        player = Player.objects.get(name="Frank")
        self.assertEqual(player.rating, 0)
