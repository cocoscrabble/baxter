import json
from datetime import date

from django.test import TestCase

from tournaments.models import Division, Entrant, Player, ResultSlip, Tournament
from tournaments.tournament_export import ExportTournament, export_tournament
from users.models import User


class TournamentExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(username="owner", password="pw")
        cls.tournament = Tournament.objects.create(
            name="Spring Open",
            location="Boston",
            start_date=date(2026, 6, 4),
            owner=owner,
        )
        cls.division = Division.objects.create(name="A", tournament=cls.tournament)

        cls.alice = Player.objects.create(player_number="A100", name="Alice", rating=1750)
        cls.bob = Player.objects.create(
            player_number="T-1", name="Bob", rating=0, is_provisional=True
        )
        cls.e_alice = Entrant.objects.create(division=cls.division, player=cls.alice, number=1)
        cls.e_bob = Entrant.objects.create(division=cls.division, player=cls.bob, number=2)

        ResultSlip.objects.create(
            division=cls.division,
            round=1,
            winner=cls.e_alice,
            winner_score=450,
            loser=cls.e_bob,
            loser_score=380,
            winner_started=True,
        )

    def bundle(self):
        return json.loads(export_tournament(self.tournament))

    def test_tournament_header(self):
        data = self.bundle()
        self.assertEqual(data["name"], "Spring Open")
        self.assertEqual(data["location"], "Boston")
        self.assertEqual(data["start_date"], "2026-06-04")

    def test_players_listed_once_with_provisional_flag(self):
        players = {p["player_number"]: p for p in self.bundle()["players"]}
        self.assertEqual(set(players), {"A100", "T-1"})
        self.assertFalse(players["A100"]["provisional"])
        self.assertTrue(players["T-1"]["provisional"])
        self.assertEqual(players["A100"]["rating"], 1750)

    def test_entrants_reference_players_by_number(self):
        division = self.bundle()["divisions"][0]
        self.assertEqual(division["name"], "A")
        entrants = {e["number"]: e["player_number"] for e in division["entrants"]}
        self.assertEqual(entrants, {1: "A100", 2: "T-1"})

    def test_results_reference_players_by_number(self):
        result = self.bundle()["divisions"][0]["results"][0]
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["winner"], "A100")
        self.assertEqual(result["winner_score"], 450)
        self.assertEqual(result["loser"], "T-1")
        self.assertEqual(result["loser_score"], 380)
        self.assertTrue(result["winner_started"])

    def test_player_in_two_divisions_appears_once(self):
        division_b = Division.objects.create(name="B", tournament=self.tournament)
        Entrant.objects.create(division=division_b, player=self.alice, number=1)
        numbers = [p["player_number"] for p in self.bundle()["players"]]
        self.assertEqual(numbers.count("A100"), 1)

    def test_excludes_test_divisions(self):
        test_div = Division.objects.create(
            name="Z", tournament=self.tournament, is_test=True
        )
        Entrant.objects.create(division=test_div, player=self.alice, number=1)
        names = [d["name"] for d in self.bundle()["divisions"]]
        self.assertNotIn("Z", names)

    def test_excludes_deleted_divisions(self):
        deleted = Division.objects.create(name="Y", tournament=self.tournament)
        Entrant.objects.create(division=deleted, player=self.alice, number=1)
        deleted.soft_delete()
        names = [d["name"] for d in self.bundle()["divisions"]]
        self.assertNotIn("Y", names)

    def test_from_db_returns_dataclass(self):
        export = ExportTournament.from_db(self.tournament)
        self.assertIsInstance(export, ExportTournament)
        self.assertEqual(len(export.divisions), 1)
        self.assertEqual(len(export.players), 2)
