from datetime import date

from django.db import IntegrityError
from django.test import TestCase

from tournaments.models import Division, Entrant, Player, ResultSlip, Tournament
from users.models import User


class TournamentModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.editor = User.objects.create_user(username="editor", password="testpass123")
        self.other_user = User.objects.create_user(username="other", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )

    def test_str_returns_name(self):
        self.assertEqual(str(self.tournament), "Test Tournament")

    def test_get_absolute_url(self):
        url = self.tournament.get_absolute_url()
        self.assertEqual(url, f"/tournaments/{self.tournament.pk}/")

    def test_owner_can_edit(self):
        self.assertTrue(self.tournament.can_edit(self.owner))

    def test_editor_can_edit(self):
        self.tournament.editors.add(self.editor)
        self.assertTrue(self.tournament.can_edit(self.editor))

    def test_other_user_cannot_edit(self):
        self.assertFalse(self.tournament.can_edit(self.other_user))

    def test_ordering_by_start_date_descending(self):
        tournament2 = Tournament.objects.create(
            name="Earlier Tournament",
            location="Location",
            start_date=date(2026, 1, 1),
            owner=self.owner,
        )
        tournaments = list(Tournament.objects.all())
        self.assertEqual(tournaments[0], self.tournament)
        self.assertEqual(tournaments[1], tournament2)


class DivisionModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )

    def test_str_returns_name(self):
        division = Division.objects.create(name="Open", tournament=self.tournament)
        self.assertEqual(str(division), "Open")

    def test_unique_together_tournament_and_name(self):
        Division.objects.create(name="Open", tournament=self.tournament)
        with self.assertRaises(IntegrityError):
            Division.objects.create(name="Open", tournament=self.tournament)

    def test_same_name_different_tournament_allowed(self):
        tournament2 = Tournament.objects.create(
            name="Another Tournament",
            location="Location",
            start_date=date(2026, 4, 1),
            owner=self.owner,
        )
        Division.objects.create(name="Open", tournament=self.tournament)
        Division.objects.create(name="Open", tournament=tournament2)
        self.assertEqual(Division.objects.filter(name="Open").count(), 2)

    def test_ordering_by_name(self):
        Division.objects.create(name="Zeta", tournament=self.tournament)
        Division.objects.create(name="Alpha", tournament=self.tournament)
        divisions = list(self.tournament.divisions.all())
        self.assertEqual(divisions[0].name, "Alpha")
        self.assertEqual(divisions[1].name, "Zeta")


class PlayerModelTests(TestCase):
    def test_str_returns_name(self):
        player = Player.objects.create(
            name="John Doe",
            player_number="12345",
            rating=1500,
        )
        self.assertEqual(str(player), "John Doe")

    def test_ordering_by_name(self):
        Player.objects.create(name="Zara", player_number="001", rating=1500)
        Player.objects.create(name="Alice", player_number="002", rating=1600)
        players = list(Player.objects.all())
        self.assertEqual(players[0].name, "Alice")
        self.assertEqual(players[1].name, "Zara")


class EntrantModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.division = Division.objects.create(name="Open", tournament=self.tournament)
        self.player = Player.objects.create(
            name="John Doe",
            player_number="12345",
            rating=1500,
        )

    def test_str_returns_number_and_player_name(self):
        entrant = Entrant.objects.create(
            division=self.division,
            player=self.player,
            number=1,
        )
        self.assertEqual(str(entrant), "1: John Doe")

    def test_unique_together_division_and_number(self):
        player2 = Player.objects.create(name="Jane Doe", player_number="54321", rating=1400)
        Entrant.objects.create(division=self.division, player=self.player, number=1)
        with self.assertRaises(IntegrityError):
            Entrant.objects.create(division=self.division, player=player2, number=1)

    def test_same_number_different_division_allowed(self):
        division2 = Division.objects.create(name="Novice", tournament=self.tournament)
        player2 = Player.objects.create(name="Jane Doe", player_number="54321", rating=1400)
        Entrant.objects.create(division=self.division, player=self.player, number=1)
        Entrant.objects.create(division=division2, player=player2, number=1)
        self.assertEqual(Entrant.objects.filter(number=1).count(), 2)

    def test_ordering_by_number(self):
        player2 = Player.objects.create(name="Jane Doe", player_number="54321", rating=1400)
        Entrant.objects.create(division=self.division, player=player2, number=5)
        Entrant.objects.create(division=self.division, player=self.player, number=1)
        entrants = list(self.division.entrants.all())
        self.assertEqual(entrants[0].number, 1)
        self.assertEqual(entrants[1].number, 5)


class ResultSlipModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.division = Division.objects.create(name="Open", tournament=self.tournament)
        self.player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
        self.player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
        self.entrant1 = Entrant.objects.create(
            division=self.division, player=self.player1, number=1
        )
        self.entrant2 = Entrant.objects.create(
            division=self.division, player=self.player2, number=2
        )

    def test_str_returns_formatted_result(self):
        slip = ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        self.assertEqual(str(slip), "R1: Alice 450-380 Bob")

    def test_ordering_by_round(self):
        ResultSlip.objects.create(
            division=self.division,
            round=3,
            winner=self.entrant1,
            winner_score=400,
            loser=self.entrant2,
            loser_score=350,
            winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant2,
            winner_score=420,
            loser=self.entrant1,
            loser_score=390,
            winner_started=False,
        )
        slips = list(self.division.result_slips.all())
        self.assertEqual(slips[0].round, 1)
        self.assertEqual(slips[1].round, 3)
