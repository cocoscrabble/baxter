from datetime import date

from django.db import IntegrityError
from django.test import TestCase

from tournaments.models import Division, DivisionSettings, Entrant, Player, ResultSlip, Tournament, next_temp_player_number
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
        self.assertEqual(url, f"/tournaments/{self.tournament.slug}/")

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


class NextTempPlayerNumberTests(TestCase):
    def test_no_players(self):
        self.assertEqual(next_temp_player_number(), "T-1")

    def test_sequences_over_existing_temp_numbers(self):
        Player.objects.create(name="A", player_number="T-1", rating=1500, is_provisional=True)
        Player.objects.create(name="B", player_number="T-2", rating=1500, is_provisional=True)
        self.assertEqual(next_temp_player_number(), "T-3")

    def test_ignores_registry_players(self):
        # Canonical registry numbers must not influence the temp sequence.
        Player.objects.create(name="A", player_number="A100", rating=1500)
        Player.objects.create(name="B", player_number="101", rating=1500)
        self.assertEqual(next_temp_player_number(), "T-1")

    def test_fills_gap_above_current_max(self):
        Player.objects.create(name="A", player_number="T-5", rating=1500, is_provisional=True)
        self.assertEqual(next_temp_player_number(), "T-6")


class SlugTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="o", password="p")

    def _tournament(self, name):
        return Tournament.objects.create(
            name=name, location="x", start_date=date(2026, 1, 1), owner=self.owner
        )

    def test_tournament_slug_from_name(self):
        t = self._tournament("Spring Open 2026")
        self.assertEqual(t.slug, "spring-open-2026")

    def test_tournament_slug_deduped_globally(self):
        a = self._tournament("Open")
        b = self._tournament("Open")
        self.assertEqual(a.slug, "open")
        self.assertEqual(b.slug, "open-2")

    def test_reserved_slug_avoided(self):
        t = self._tournament("Create")
        self.assertEqual(t.slug, "create-2")

    def test_empty_slugify_falls_back(self):
        t = self._tournament("!!!")
        self.assertEqual(t.slug, "tournament")

    def test_division_slug_deduped_within_tournament_only(self):
        t1 = self._tournament("T1")
        t2 = self._tournament("T2")
        d1 = Division.objects.create(name="A", tournament=t1)
        d1b = Division.objects.create(name="A B", tournament=t1)  # also slugifies near "a"
        d2 = Division.objects.create(name="A", tournament=t2)
        self.assertEqual(d1.slug, "a")
        self.assertEqual(d2.slug, "a")  # different tournament, no clash
        self.assertEqual(d1b.slug, "a-b")

    def test_rename_resyncs_slug_and_records_alias(self):
        t = self._tournament("Old Name")
        d = Division.objects.create(name="Open", tournament=t)
        old_t_slug, old_d_slug = t.slug, d.slug

        t.name = "New Name"
        t.save()
        d.name = "Champs"
        d.save()

        self.assertEqual(t.slug, "new-name")
        self.assertEqual(d.slug, "champs")
        self.assertTrue(t.slug_aliases.filter(slug=old_t_slug).exists())
        self.assertTrue(d.slug_aliases.filter(slug=old_d_slug).exists())

    def test_rename_persists_slug_with_update_fields(self):
        d = Division.objects.create(name="Open", tournament=self._tournament("T"))
        d.name = "Closed"
        d.save(update_fields=["name"])
        d.refresh_from_db()
        self.assertEqual(d.slug, "closed")
