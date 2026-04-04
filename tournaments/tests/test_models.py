from datetime import date

from django.db import IntegrityError
from django.test import TestCase

from tournaments.models import Division, DivisionSettings, Entrant, Player, ResultSlip, Tournament, next_player_number
from users.models import User


def setUpTournament(target):
    """Common test setup: owner, other user, tournament, division, 2 players + entrants."""
    target.owner = User.objects.create_user(username="owner", password="testpass123")
    target.other = User.objects.create_user(username="other", password="testpass123")
    target.tournament = Tournament.objects.create(
        name="Test Tournament",
        location="Test Location",
        start_date=date(2026, 3, 15),
        owner=target.owner,
    )
    target.tournament.editors.add(target.owner)
    target.division = Division.objects.create(name="Open", tournament=target.tournament)
    target.player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
    target.player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
    target.entrant1 = Entrant.objects.create(
        division=target.division, player=target.player1, number=1
    )
    target.entrant2 = Entrant.objects.create(
        division=target.division, player=target.player2, number=2
    )


class TournamentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.editor = User.objects.create_user(username="editor", password="testpass123")

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
        self.assertFalse(self.tournament.can_edit(self.other))


class DivisionModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_str_returns_name(self):
        self.assertEqual(str(self.division), "Open")

    def test_max_round_with_no_results(self):
        division = Division.objects.create(name="Empty", tournament=self.tournament)
        self.assertEqual(division.max_round(), 0)

    def test_max_round_with_results(self):
        division = Division.objects.create(name="WithResults", tournament=self.tournament)
        entrant1 = Entrant.objects.create(division=division, player=self.player1, number=1)
        entrant2 = Entrant.objects.create(division=division, player=self.player2, number=2)
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
        setUpTournament(cls)
        cls.division.entrants.all().delete()
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
        setUpTournament(cls)

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
        setUpTournament(cls)

    def test_str(self):
        settings = DivisionSettings.objects.create(division=self.division)
        self.assertEqual(str(settings), "Settings for Open")


class NextPlayerNumberTests(TestCase):
    def test_no_players(self):
        self.assertEqual(next_player_number(), "1")

    def test_numeric_only(self):
        Player.objects.create(name="A", player_number="100", rating=1500)
        Player.objects.create(name="B", player_number="101", rating=1500)
        self.assertEqual(next_player_number(), "102")

    def test_alpha_prefix(self):
        Player.objects.create(name="A", player_number="A100", rating=1500)
        Player.objects.create(name="B", player_number="A101", rating=1500)
        self.assertEqual(next_player_number(), "A102")

    def test_mixed_prefixes_uses_last_lexically(self):
        Player.objects.create(name="A", player_number="A50", rating=1500)
        Player.objects.create(name="B", player_number="B10", rating=1500)
        # "B10" sorts after "A50" lexically
        self.assertEqual(next_player_number(), "B11")
