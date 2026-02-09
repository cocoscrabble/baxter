from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Division, Entrant, Player, ResultSlip, Tournament
from users.models import User


class TournamentListViewTests(TestCase):
    def test_get_tournament_list(self):
        response = self.client.get(reverse("tournament_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/tournament_list.html")

    def test_lists_tournaments(self):
        owner = User.objects.create_user(username="owner", password="testpass123")
        Tournament.objects.create(
            name="Tournament 1",
            location="Location 1",
            start_date=date(2026, 3, 15),
            owner=owner,
        )
        Tournament.objects.create(
            name="Tournament 2",
            location="Location 2",
            start_date=date(2026, 4, 15),
            owner=owner,
        )
        response = self.client.get(reverse("tournament_list"))
        self.assertContains(response, "Tournament 1")
        self.assertContains(response, "Tournament 2")


class TournamentDetailViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="testpass123")
        cls.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=cls.owner,
        )

    def test_get_tournament_detail(self):
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/tournament_detail.html")
        self.assertContains(response, "Test Tournament")

    def test_shows_edit_link_for_owner(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        edit_url = reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        self.assertContains(response, edit_url)

    def test_no_edit_link_for_anonymous(self):
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        edit_url = reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        self.assertNotContains(response, edit_url)

    def test_division_links(self):
        division = Division.objects.create(name="Open", tournament=self.tournament)
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertContains(response, "Open")
        self.assertContains(response, reverse("division_detail", kwargs={"pk": division.pk}))


class TournamentCreateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser", password="testpass123")

    def test_requires_login(self):
        response = self.client.get(reverse("tournament_create"))
        self.assertRedirects(
            response, f"{reverse('login')}?next={reverse('tournament_create')}"
        )

    def test_get_create_page(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("tournament_create"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/tournament_form.html")

    def test_create_tournament(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.post(
            reverse("tournament_create"),
            {
                "name": "New Tournament",
                "location": "New Location",
                "start_date": "2026-05-01",
                "editor_usernames": "",
                "division_names": "",
            },
        )
        tournament = Tournament.objects.get(name="New Tournament")
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": tournament.pk})
        )
        self.assertEqual(tournament.owner, self.user)

    def test_create_tournament_with_divisions(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.post(
            reverse("tournament_create"),
            {
                "name": "New Tournament",
                "location": "New Location",
                "start_date": "2026-05-01",
                "editor_usernames": "",
                "division_names": "Open\nNovice",
            },
        )
        tournament = Tournament.objects.get(name="New Tournament")
        self.assertEqual(tournament.divisions.count(), 2)


class TournamentUpdateViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.editor = User.objects.create_user(username="editor", password="testpass123")
        self.other = User.objects.create_user(username="other", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.tournament.editors.add(self.owner, self.editor)

    def test_requires_login(self):
        response = self.client.get(
            reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        )
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('tournament_edit', kwargs={'pk': self.tournament.pk})}",
        )

    def test_owner_can_edit(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_editor_can_edit(self):
        self.client.login(username="editor", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_other_user_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_update_tournament(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("tournament_edit", kwargs={"pk": self.tournament.pk}),
            {
                "name": "Updated Tournament",
                "location": "Updated Location",
                "start_date": "2026-06-01",
                "editor_usernames": "",
                "division_names": "",
            },
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.name, "Updated Tournament")


class TournamentDeleteViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.editor = User.objects.create_user(username="editor", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.tournament.editors.add(self.owner, self.editor)

    def test_requires_login(self):
        response = self.client.get(
            reverse("tournament_delete", kwargs={"pk": self.tournament.pk})
        )
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('tournament_delete', kwargs={'pk': self.tournament.pk})}",
        )

    def test_owner_can_delete(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_delete", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/tournament_confirm_delete.html")

    def test_editor_cannot_delete(self):
        self.client.login(username="editor", password="testpass123")
        response = self.client.get(
            reverse("tournament_delete", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_tournament(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("tournament_delete", kwargs={"pk": self.tournament.pk})
        )
        self.assertRedirects(response, reverse("tournament_list"))
        self.assertFalse(Tournament.objects.filter(pk=self.tournament.pk).exists())


class DivisionDetailViewTests(TestCase):
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

    def test_get_division_detail(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/division_detail.html")
        self.assertContains(response, "Open")

    def test_shows_entrants(self):
        player = Player.objects.create(name="Alice", player_number="001", rating=1600)
        Entrant.objects.create(division=self.division, player=player, number=1)
        response = self.client.get(
            reverse("division_entrants", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Alice")

    def test_shows_results(self):
        player1 = Player.objects.create(name="Alice", player_number="001", rating=1600)
        player2 = Player.objects.create(name="Bob", player_number="002", rating=1500)
        entrant1 = Entrant.objects.create(division=self.division, player=player1, number=1)
        entrant2 = Entrant.objects.create(division=self.division, player=player2, number=2)
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=entrant1,
            winner_score=450,
            loser=entrant2,
            loser_score=380,
            winner_started=True,
        )
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "450")
        self.assertContains(response, "380")

    def test_shows_add_result_link(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Add Result")
        self.assertContains(
            response, reverse("resultslip_create", kwargs={"pk": self.division.pk})
        )


class ResultSlipCreateViewTests(TestCase):
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

    def test_get_create_page(self):
        response = self.client.get(
            reverse("resultslip_create", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/resultslip_form.html")

    def test_create_result_slip(self):
        response = self.client.post(
            reverse("resultslip_create", kwargs={"pk": self.division.pk}),
            {
                "round": 1,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser": self.entrant2.pk,
                "loser_score": 380,
                "winner_started": True,
            },
        )
        self.assertRedirects(
            response, reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(self.division.result_slips.count(), 1)

    def test_form_shows_division_entrants_only(self):
        other_division = Division.objects.create(name="Novice", tournament=self.tournament)
        other_player = Player.objects.create(name="Charlie", player_number="003", rating=1400)
        Entrant.objects.create(division=other_division, player=other_player, number=1)

        response = self.client.get(
            reverse("resultslip_create", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")
        self.assertNotContains(response, "Charlie")

    def test_invalid_same_winner_loser(self):
        response = self.client.post(
            reverse("resultslip_create", kwargs={"pk": self.division.pk}),
            {
                "round": 1,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser": self.entrant1.pk,
                "loser_score": 380,
                "winner_started": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Winner and loser must be different")
