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
        cls.e_alice = Entrant.enter(cls.division, cls.alice, 1)
        cls.e_bob = Entrant.enter(cls.division, cls.bob, 2)

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


class ExportedRatingSnapshotTests(TestCase):
    """The bundle carries the rating the division was *seeded* from.

    The player record has almost certainly drifted by the time a tournament is
    exported — a WESPA refresh, a roster sync, a new rating period — so
    re-deriving the seed from the player would answer a different question.
    """

    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user(username="owner-snap", password="pw")
        cls.tournament = Tournament.objects.create(
            name="Snapshot Open", location="X",
            start_date=date(2026, 6, 4), owner=owner,
        )
        cls.division = Division.objects.create(
            name="A", tournament=cls.tournament
        )
        cls.player = Player.objects.create(
            player_number="A200", name="Drifty", rating=1500
        )
        cls.entrant = Entrant.enter(cls.division, cls.player, 1)

    def _entrant(self):
        data = json.loads(export_tournament(self.tournament))
        return data["divisions"][0]["entrants"][0]

    def test_the_seeded_rating_and_its_source_are_exported(self):
        row = self._entrant()
        self.assertEqual(row["rating"], 1500)
        self.assertEqual(row["rating_source"], "coco")

    def test_it_does_not_follow_the_player_afterwards(self):
        self.player.rating = 1900
        self.player.save(update_fields=["rating"])
        row = self._entrant()
        self.assertEqual(row["rating"], 1500, "the seed, not today's rating")
        # The player block still reports the current one — a different question,
        # answered separately.
        data = json.loads(export_tournament(self.tournament))
        self.assertEqual(data["players"][0]["rating"], 1900)

    def test_a_hand_set_rating_is_marked_manual(self):
        entrant = self.division.entrants.get()
        entrant.rating, entrant.rating_source = 1234, "manual"
        entrant.save(update_fields=["rating", "rating_source"])
        row = self._entrant()
        self.assertEqual((row["rating"], row["rating_source"]), (1234, "manual"))
