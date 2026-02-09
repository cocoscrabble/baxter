from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Division, DivisionSettings, Entrant, Player, ResultSlip, Tournament
from users.models import User


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


class TournamentCreateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser", password="testpass123")

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
        self.client.post(
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


class DivisionDetailLatestResultsTests(TestCase):
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

    def test_shows_only_max_round_results(self):
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=400, loser=self.entrant2, loser_score=350, winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division, round=2, winner=self.entrant2,
            winner_score=420, loser=self.entrant1, loser_score=390, winner_started=False,
        )
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        # Round 2 scores should appear, round 1 scores should not
        self.assertContains(response, "420")
        self.assertNotContains(response, "400-350")

    def test_settings_link_shown_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertContains(
            response, reverse("division_settings", kwargs={"pk": self.division.pk})
        )

    def test_settings_link_hidden_for_anonymous(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertNotContains(response, "Settings")


class DivisionStandingsViewTests(TestCase):
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

    def test_standings_with_results(self):
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        response = self.client.get(
            reverse("division_standings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_standings_for_specific_round(self):
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division, round=2, winner=self.entrant2,
            winner_score=420, loser=self.entrant1, loser_score=390, winner_started=False,
        )
        response = self.client.get(
            reverse("division_standings_round", kwargs={"pk": self.division.pk, "round": 1})
        )
        self.assertEqual(response.status_code, 200)
        # After round 1 only, Alice has 1 win
        self.assertContains(response, "After round 1")

    def test_round_links(self):
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division, round=2, winner=self.entrant2,
            winner_score=420, loser=self.entrant1, loser_score=390, winner_started=False,
        )
        response = self.client.get(
            reverse("division_standings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(
            response,
            reverse("division_standings_round", kwargs={"pk": self.division.pk, "round": 1}),
        )


class DivisionSettingsViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.other = User.objects.create_user(username="other", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.tournament.editors.add(self.owner)
        self.division = Division.objects.create(name="Open", tournament=self.tournament)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_redirects_to_rounds_when_no_settings(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertRedirects(
            response,
            reverse("division_settings_rounds", kwargs={"pk": self.division.pk}),
        )

    def test_redirects_to_edit_when_settings_exist(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "Swiss", "start_round": 0}],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertRedirects(
            response,
            reverse("division_settings_edit", kwargs={"pk": self.division.pk}),
        )

    def test_redirects_to_rounds_when_settings_empty(self):
        DivisionSettings.objects.create(division=self.division, round_pairings=[])
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertRedirects(
            response,
            reverse("division_settings_rounds", kwargs={"pk": self.division.pk}),
        )


class DivisionRoundCountViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.tournament.editors.add(self.owner)
        self.division = Division.objects.create(name="Open", tournament=self.tournament)

    def test_post_redirects_to_edit(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_settings_rounds", kwargs={"pk": self.division.pk}),
            {"num_rounds": 5},
        )
        edit_url = reverse("division_settings_edit", kwargs={"pk": self.division.pk})
        self.assertRedirects(response, f"{edit_url}?rounds=5")


class DivisionSettingsEditViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            location="Test Location",
            start_date=date(2026, 3, 15),
            owner=self.owner,
        )
        self.tournament.editors.add(self.owner)
        self.division = Division.objects.create(name="Open", tournament=self.tournament)

    def test_get_with_rounds_param(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings_edit", kwargs={"pk": self.division.pk})
            + "?rounds=3"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/division_settings_edit.html")
        # Should have 3 forms in the formset
        self.assertEqual(len(response.context["formset"]), 3)

    def test_get_with_existing_settings(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "Swiss", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings_edit", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["formset"]), 2)

    def test_get_without_data_redirects_to_rounds(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings_edit", kwargs={"pk": self.division.pk})
        )
        self.assertRedirects(
            response,
            reverse("division_settings_rounds", kwargs={"pk": self.division.pk}),
        )

    def test_post_saves_settings(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_settings_edit", kwargs={"pk": self.division.pk}),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-round": "1",
                "form-0-pairing_type": "Swiss",
                "form-0-start_round": "0",
                "form-1-round": "2",
                "form-1-pairing_type": "KotH",
                "form-1-start_round": "1",
            },
        )
        self.assertRedirects(
            response,
            reverse("division_detail", kwargs={"pk": self.division.pk}),
        )
        settings = DivisionSettings.objects.get(division=self.division)
        self.assertEqual(len(settings.round_pairings), 2)
        self.assertEqual(settings.round_pairings[0]["pairing"], "Swiss")
        self.assertEqual(settings.round_pairings[1]["pairing"], "KotH")

    def test_post_invalid_stays_on_page(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_settings_edit", kwargs={"pk": self.division.pk}),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-round": "1",
                "form-0-pairing_type": "Swiss",
                "form-0-start_round": "1",  # invalid: not less than round
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DivisionSettings.objects.filter(division=self.division).exists())


class DivisionPairingsViewTests(TestCase):
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

    def test_no_settings_shows_not_configured(self):
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Division settings have not been configured")

    def test_empty_settings_shows_no_pairings_configured(self):
        DivisionSettings.objects.create(division=self.division, round_pairings=[])
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "No round pairings configured")

    def test_all_rounds_finished_shows_no_upcoming(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "No upcoming pairings available")

    def test_with_settings_shows_pairings(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        pairings = response.context["pairings"]
        self.assertEqual(len(pairings), 1)
        round_num, round_pairings = pairings[0]
        self.assertEqual(round_num, 1)
        self.assertEqual(len(round_pairings), 1)
        names = {round_pairings[0].first.name, round_pairings[0].second.name}
        self.assertEqual(names, {"Alice", "Bob"})
        self.assertContains(response, "Round 1")
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_finished_rounds_not_shown(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "KotH", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        pairings = response.context["pairings"]
        # Round 1 is finished, only round 2 should appear
        self.assertEqual(len(pairings), 1)
        self.assertEqual(pairings[0][0], 2)

    def test_pairings_link_on_division_detail(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertContains(
            response,
            reverse("division_pairings", kwargs={"pk": self.division.pk}),
        )
