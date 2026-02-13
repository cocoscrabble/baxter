from datetime import date

from django.db import IntegrityError
from django.test import TestCase

from tournaments.models import Division, DivisionSettings, Entrant, Player, ResultSlip, Tournament
from users.models import User


class TournamentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.editor = User.objects.create_user(username="editor", password="testpass123")
        cls.other_user = User.objects.create_user(username="other", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
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


class DivisionModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
        )

    def test_str_returns_name(self):
        division = Division.objects.create(name="Open", tournament=self.tournament)
        self.assertEqual(str(division), "Open")

    def test_max_round_with_no_results(self):
        division = Division.objects.create(name="Empty", tournament=self.tournament)
        self.assertEqual(division.max_round(), 0)

    def test_max_round_with_results(self):
        division = Division.objects.create(name="WithResults", tournament=self.tournament)
        player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
        player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
        entrant1 = Entrant.objects.create(division=division, player=player1, number=1)
        entrant2 = Entrant.objects.create(division=division, player=player2, number=2)
        for r in [1, 3, 2]:
            ResultSlip.objects.create(
                division=division,
                round=r,
                winner=entrant1,
                winner_score=400,
                loser=entrant2,
                loser_score=350,
                winner_started=True,
            )
        self.assertEqual(division.max_round(), 3)


class EntrantModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)
        cls.player = Player.objects.create(
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

    def test_duplicate_player_in_division_is_rejected(self):
        Entrant.objects.create(division=self.division, player=self.player, number=1)
        with self.assertRaises(IntegrityError):
            Entrant.objects.create(division=self.division, player=self.player, number=2)


class ResultSlipModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)
        cls.player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
        cls.player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
        cls.entrant1 = Entrant.objects.create(
            division=cls.division, player=cls.player1, number=1
        )
        cls.entrant2 = Entrant.objects.create(
            division=cls.division, player=cls.player2, number=2
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

    def test_winner_and_loser_name(self):
        slip = ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        self.assertEqual(slip.winner_name, "Alice")
        self.assertEqual(slip.loser_name, "Bob")


class DivisionSettingsModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
        )
        cls.division = Division.objects.create(name="Open", tournament=cls.tournament)

    def test_str(self):
        settings = DivisionSettings.objects.create(division=self.division)
        self.assertEqual(str(settings), "Settings for Open")

