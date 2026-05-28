import json
from datetime import date

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.models import Division, DivisionSettings, Entrant, FixedPairing, FixedTable, Pairing, Player, ResultSlip, RoundPairings, Tournament
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


@tag("slow")
class TournamentDetailViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

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


@tag("slow")
class TournamentCreateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_create_tournament(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("tournament_create"),
            {
                "name": "New Tournament",
                "location": "New Location",
                "start_date": "2026-05-01",
                "editor_usernames": "",
            },
        )
        tournament = Tournament.objects.get(name="New Tournament")
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": tournament.pk})
        )
        self.assertEqual(tournament.owner, self.owner)


@tag("slow")
class DivisionCreateDeleteViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)

    def test_create_division(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_create", kwargs={"tournament_pk": self.tournament.pk}),
            {"name": "Open", "is_test": "0"},
        )
        self.assertTrue(self.tournament.divisions.filter(name="Open", is_test=False).exists())

    def test_create_test_division(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_create", kwargs={"tournament_pk": self.tournament.pk}),
            {"name": "Sandbox", "is_test": "1"},
        )
        self.assertTrue(self.tournament.divisions.filter(name="Sandbox", is_test=True).exists())

    def test_create_division_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_create", kwargs={"tournament_pk": self.tournament.pk}),
            {"name": "Novice", "is_test": "0"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.tournament.divisions.filter(name="Novice").exists())

    def test_create_redirects_to_tournament_detail(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_create", kwargs={"tournament_pk": self.tournament.pk}),
            {"name": "Open", "is_test": "0"},
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )

    def test_delete_division_soft_deletes(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(reverse("division_delete", kwargs={"pk": self.division.pk}))
        self.assertFalse(Division.objects.filter(pk=self.division.pk).exists())
        self.assertTrue(Division.all_objects.filter(pk=self.division.pk, is_deleted=True).exists())

    def test_delete_division_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_delete", kwargs={"pk": self.division.pk}),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Division.objects.filter(pk=self.division.pk).exists())

    def test_delete_redirects_to_tournament_detail(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_delete", kwargs={"pk": self.division.pk}),
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )

    def test_restore_division(self):
        self.division.soft_delete()
        self.client.login(username="owner", password="testpass123")
        self.client.post(reverse("division_restore", kwargs={"pk": self.division.pk}))
        self.assertTrue(Division.objects.filter(pk=self.division.pk, is_deleted=False).exists())

    def test_restore_division_non_editor_forbidden(self):
        self.division.soft_delete()
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_restore", kwargs={"pk": self.division.pk}),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Division.all_objects.filter(pk=self.division.pk, is_deleted=True).exists())


@tag("slow")
class TournamentUpdateViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.editor = User.objects.create_user(username="editor", password="testpass123")
        self.tournament.editors.add(self.editor)

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
            },
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.name, "Updated Tournament")


@tag("slow")
class TournamentDeleteViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.editor = User.objects.create_user(username="editor", password="testpass123")
        self.tournament.editors.add(self.editor)

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


@tag("slow")
class CreatePlayerViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="owner", password="testpass123")

    def test_create_player(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("create_player"),
            json.dumps({"name": "NewPlayer", "rating": 1500}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["label"], "NewPlayer")
        self.assertTrue(Player.objects.filter(name="NewPlayer").exists())

    def test_duplicate_name_rejected(self):
        Player.objects.create(name="Alice", player_number="001", rating=1600)
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("create_player"),
            json.dumps({"name": "alice"}),  # case-insensitive
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["error"])

    def test_empty_name_rejected(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("create_player"),
            json.dumps({"name": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_default_rating_zero(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("create_player"),
            json.dumps({"name": "NoRating"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Player.objects.get(name="NoRating").rating, 0)


class BulkImportEntrantsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def _upload(self, csv_content):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.login(username="owner", password="testpass123")
        f = SimpleUploadedFile("import.csv", csv_content.encode("utf-8"), content_type="text/csv")
        return self.client.post(
            reverse("bulk_import_entrants", kwargs={"pk": self.division.pk}),
            {"csv_file": f},
        )

    def test_import_new_players(self):
        response = self._upload("Charlie,1400\nDave,1300\n")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], 2)
        self.assertEqual(len(data["created"]), 2)
        self.assertTrue(Player.objects.filter(name="Charlie").exists())
        self.assertEqual(self.division.entrants.count(), 4)  # 2 existing + 2 new

    def test_import_existing_player_matched(self):
        # Alice already exists as player1 but is already an entrant
        response = self._upload("Alice,1600\n")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], 0)
        self.assertEqual(len(data["skipped"]), 1)

    def test_import_existing_player_not_in_division(self):
        other_player = Player.objects.create(name="Eve", player_number="003", rating=1200)
        response = self._upload("Eve,1200\n")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], 1)
        self.assertEqual(len(data["matched"]), 1)

    def test_duplicate_in_csv_rejected(self):
        response = self._upload("Charlie,1400\nCharlie,1300\n")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertTrue(any("duplicate" in e.lower() for e in data["errors"]))

    def test_name_only_import(self):
        response = self._upload("Charlie\n")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], 1)
        self.assertEqual(Player.objects.get(name="Charlie").rating, 0)

    def test_entrant_numbers_append(self):
        # Existing entrants are numbered 1, 2
        response = self._upload("Charlie,1400\n")
        data = response.json()
        self.assertTrue(data["ok"])
        entrant = self.division.entrants.get(player__name="Charlie")
        self.assertEqual(entrant.number, 3)


class ResultSlipCreateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.rp = RoundPairings.objects.create(
            division=cls.division, round=1, status=RoundPairings.PUBLISHED,
        )
        cls.pairing = Pairing.objects.create(
            division=cls.division, round=1, round_pairings=cls.rp,
            first=cls.entrant1, second=cls.entrant2,
            table=1,
        )

    def test_create_result_slip(self):
        response = self.client.post(
            reverse("resultslip_create", kwargs={"pk": self.division.pk}),
            {
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Result saved")
        self.assertEqual(self.division.result_slips.count(), 1)
        rs = self.division.result_slips.first()
        self.assertEqual(rs.pairing, self.pairing)

    def test_form_shows_pairing_options(self):
        response = self.client.get(
            reverse("resultslip_create", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_invalid_winner_not_in_pairing(self):
        other_player = Player.objects.create(name="Charlie", player_number="003", rating=1400)
        other_entrant = Entrant.objects.create(division=self.division, player=other_player, number=3)
        response = self.client.post(
            reverse("resultslip_create", kwargs={"pk": self.division.pk}),
            {
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": other_entrant.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Winner must be one of the players in the pairing")


@tag("slow")
class DivisionDetailLatestResultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

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


@tag("slow")
class DivisionStandingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

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


@tag("slow")
class DivisionSettingsEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)

    def test_get_with_rounds_param(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
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
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["formset"]), 2)

    def test_get_without_data_shows_empty_formset(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["formset"]), 0)
        self.assertEqual(response.context["round_count_form"].initial["num_rounds"], 0)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_post_saves_settings(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_settings", kwargs={"pk": self.division.pk}),
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

    def test_increase_rounds_preserves_existing(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "Swiss", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
            + "?rounds=4"
        )
        formset = response.context["formset"]
        self.assertEqual(len(formset), 4)
        # Existing rounds preserved
        self.assertEqual(formset[0].initial["pairing_type"], "Swiss")
        self.assertEqual(formset[1].initial["pairing_type"], "KotH")
        # New rounds have defaults
        self.assertEqual(formset[2].initial["round"], 3)
        self.assertEqual(formset[2].initial["pairing_type"], "")
        self.assertEqual(formset[3].initial["round"], 4)

    def test_decrease_rounds_truncates(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "Swiss", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
                {"round": 3, "pairing": "Swiss", "start_round": 2},
            ],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
            + "?rounds=1"
        )
        formset = response.context["formset"]
        self.assertEqual(len(formset), 1)
        self.assertEqual(formset[0].initial["pairing_type"], "Swiss")

    def test_round_count_form_in_context(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "Swiss", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_settings", kwargs={"pk": self.division.pk})
        )
        round_count_form = response.context["round_count_form"]
        self.assertEqual(round_count_form.initial["num_rounds"], 2)

    def test_post_invalid_stays_on_page(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_settings", kwargs={"pk": self.division.pk}),
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


@tag("slow")
class DivisionPairingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_no_pairings_configured_shows_message(self):
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertContains(response, "No round pairings configured")

    def test_with_pairings_in_db_shows_pairings(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        Pairing.objects.create(
            division=self.division, round=1,
            first=self.entrant1, second=self.entrant2, repeats=0,
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 1)
        p = round_pairings[0].pairing
        names = {p.first.name, p.second.name}
        self.assertEqual(names, {"Alice", "Bob"})
        self.assertEqual(round_pairings[0].result, "")
        self.assertContains(response, "Round 1")
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_only_stored_pairings_shown(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "KotH", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )
        # Round 1 pairing in DB, round 2 not yet generated
        Pairing.objects.create(
            division=self.division, round=1,
            first=self.entrant1, second=self.entrant2, repeats=0,
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        # Selected round should be round 1 (pairable)
        self.assertEqual(response.context["selected_round"], 1)
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 1)

    def test_generate_pairings_populates_db(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        self.client.login(username="owner", password="testpass123")
        self.client.post(reverse("generate_pairings", kwargs={"pk": self.division.pk}))
        self.assertEqual(Pairing.objects.filter(division=self.division).count(), 1)

    def test_pairings_link_on_division_detail(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertContains(
            response,
            reverse("division_pairings", kwargs={"pk": self.division.pk}),
        )


@tag("slow")
class DivisionPairingsRoundContentTests(TestCase):
    """Verify the pairings view returns the full pairings table for each round,
    including unplayed matches alongside played ones for in-progress rounds."""

    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.player3 = Player.objects.create(name="Carol", player_number="003", rating=1400)
        cls.player4 = Player.objects.create(name="Dave", player_number="004", rating=1300)
        cls.entrant3 = Entrant.objects.create(
            division=cls.division, player=cls.player3, number=3
        )
        cls.entrant4 = Entrant.objects.create(
            division=cls.division, player=cls.player4, number=4
        )
        DivisionSettings.objects.create(
            division=cls.division,
            round_pairings=[
                {"round": 1, "pairing": "KotH", "start_round": 0},
                {"round": 2, "pairing": "KotH", "start_round": 1},
            ],
        )

    def _create_round(self, round_num, status, pairs):
        """Create a RoundPairings with two Pairing rows for the given round."""
        rp = RoundPairings.objects.create(
            division=self.division, round=round_num, status=status,
        )
        pairings = []
        for table, (first, second) in enumerate(pairs, start=1):
            pairings.append(Pairing.objects.create(
                division=self.division, round=round_num, round_pairings=rp,
                first=first, second=second, table=table,
            ))
        return rp, pairings

    def _create_slip(self, round_num, pairing, winner, loser, winner_score, loser_score):
        return ResultSlip.objects.create(
            division=self.division, round=round_num, pairing=pairing,
            winner=winner, winner_score=winner_score,
            loser=loser, loser_score=loser_score, winner_started=True,
        )

    def test_in_progress_round_shows_played_and_unplayed_pairings(self):
        _, pairings = self._create_round(
            1, RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.context["selected_round"], 1)
        self.assertEqual(response.context["selected_status"], "in_progress")
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 2)
        by_table = {e.pairing.table: e for e in round_pairings}
        self.assertEqual(by_table[1].result, "450 - 380")
        self.assertEqual(by_table[2].result, "")

    def test_finished_round_shows_all_pairings_with_results(self):
        _, pairings = self._create_round(
            1, RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, pairings[1], self.entrant3, self.entrant4, 500, 400)

        # Navigate directly to round 1 so we don't get auto-routed to a pairable later round.
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={"pk": self.division.pk, "round": 1})
        )
        self.assertEqual(response.context["selected_round"], 1)
        self.assertEqual(response.context["selected_status"], "finished")
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 2)
        results = sorted(e.result for e in round_pairings)
        self.assertEqual(results, ["450 - 380", "500 - 400"])

    def test_finished_round_with_unplayed_pairing_still_lists_it(self):
        """All Pairing rows must appear even if a slip is missing for one."""
        _, pairings = self._create_round(
            1, RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        # Navigate directly to round 1: round 2 is now pairable (its prereq
        # round 1 has lifecycle status FINISHED), so default selection would
        # land on round 2 instead.
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={"pk": self.division.pk, "round": 1})
        )
        self.assertEqual(response.context["selected_status"], "finished")
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 2)
        by_table = {e.pairing.table: e for e in round_pairings}
        self.assertEqual(by_table[1].result, "450 - 380")
        self.assertEqual(by_table[2].result, "")

    def test_error_status_when_results_but_no_pairing_records(self):
        """Partial slips with no Pairing rows surfaces as error_no_pairings, not in_progress."""
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=None,
            winner=self.entrant1, winner_score=450,
            loser=self.entrant2, loser_score=380, winner_started=True,
        )
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={"pk": self.division.pk, "round": 1})
        )
        tabs_by_round = {t["round"]: t for t in response.context["round_tabs"]}
        self.assertEqual(tabs_by_round[1]["status"], "error_no_pairings")
        self.assertEqual(response.context["selected_status"], "error_no_pairings")

    def test_published_pairings_page_excludes_finished_rounds(self):
        _, r1_pairings = self._create_round(
            1, RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, r1_pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, r1_pairings[1], self.entrant3, self.entrant4, 500, 400)
        self._create_round(
            2, RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant3), (self.entrant2, self.entrant4)],
        )

        response = self.client.get(
            reverse("published_pairings", kwargs={"pk": self.division.pk})
        )
        rounds_shown = [r for r, _ in response.context["pairings"]]
        self.assertEqual(rounds_shown, [2])

    def test_published_round_shows_published_tab_status(self):
        self._create_round(
            1, RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        tabs_by_round = {t["round"]: t for t in response.context["round_tabs"]}
        self.assertEqual(tabs_by_round[1]["status"], "published")
        self.assertEqual(response.context["selected_status"], "published")

    def test_generate_button_hidden_when_round_is_published(self):
        self._create_round(
            1, RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        # Round 1 is published, so it should NOT be in the generate label.
        # Round 2 is pairable only once round 1 is finished, so generate_label
        # should be empty (round 2 has start_round=1 and round 1 isn't finished).
        self.assertNotIn("generate_label", response.context)

    def test_add_fixed_pairing_redrafts_published_round(self):
        _, pairings = self._create_round(
            1, RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        original_pks = sorted(p.pk for p in pairings)

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("add_fixed_pairing", kwargs={"pk": self.division.pk}),
            {"round": 1, "entrant1": self.entrant1.pk, "entrant2": self.entrant3.pk},
        )
        self.assertEqual(response.status_code, 302)

        rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        new_pks = sorted(p.pk for p in self.division.pairings.filter(round=1))
        self.assertNotEqual(new_pks, original_pks)
        fp = self.division.fixed_pairings.get(round_number=1)
        self.assertEqual(
            {fp.entrant1_id, fp.entrant2_id},
            {self.entrant1.pk, self.entrant3.pk},
        )
        new_pair = self.division.pairings.filter(round=1).filter(
            first__in=[self.entrant1, self.entrant3],
            second__in=[self.entrant1, self.entrant3],
        )
        self.assertEqual(new_pair.count(), 1)

    def test_add_fixed_pairing_blocked_when_round_has_results(self):
        _, pairings = self._create_round(
            1, RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("add_fixed_pairing", kwargs={"pk": self.division.pk}),
            {"round": 1, "entrant1": self.entrant3.pk, "entrant2": self.entrant4.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.division.fixed_pairings.exists())

    def test_remove_fixed_pairing_blocked_when_affected_round_has_results(self):
        _, pairings = self._create_round(
            1, RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        fp = FixedPairing.objects.create(
            division=self.division, round_number=1,
            entrant1=self.entrant3, entrant2=self.entrant4,
        )

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("remove_fixed_pairings", kwargs={"pk": self.division.pk}),
            {"keep": []},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.division.fixed_pairings.filter(pk=fp.pk).exists())

    def test_has_published_rounds_false_when_only_finished(self):
        _, pairings = self._create_round(
            1, RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, pairings[1], self.entrant3, self.entrant4, 500, 400)
        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertFalse(response.context["has_published_rounds"])

    def test_in_progress_round_selected_when_earlier_round_finished(self):
        _, r1_pairings = self._create_round(
            1, RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, r1_pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, r1_pairings[1], self.entrant3, self.entrant4, 500, 400)
        _, r2_pairings = self._create_round(
            2, RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant3), (self.entrant2, self.entrant4)],
        )
        self._create_slip(2, r2_pairings[0], self.entrant1, self.entrant3, 420, 410)

        response = self.client.get(
            reverse("division_pairings", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.context["selected_round"], 2)
        self.assertEqual(response.context["selected_status"], "in_progress")
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 2)
        by_table = {e.pairing.table: e for e in round_pairings}
        self.assertEqual(by_table[1].result, "420 - 410")
        self.assertEqual(by_table[2].result, "")


@tag("slow")
class DivisionEditResultsViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_edit_results", kwargs={"pk": self.division.pk})

    def test_editor_can_access(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_returns_json_context(self):
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=450, loser=self.entrant2, loser_score=380, winner_started=True,
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        results = json.loads(response.context["results_json"])
        entrants = json.loads(response.context["entrants_json"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["round"], 1)
        self.assertEqual(results[0]["winner"], self.entrant1.pk)
        self.assertEqual(results[0]["winner_score"], 450)
        self.assertEqual(len(entrants), 2)
        labels = {e["label"] for e in entrants}
        self.assertEqual(labels, {"Alice", "Bob"})

    def _make_pairing(self, round_num, first, second, table=1):
        return Pairing.objects.create(
            division=self.division, round=round_num,
            first=first, second=second, table=table,
        )

    def test_post_saves_results(self):
        self._make_pairing(1, self.entrant1, self.entrant2)
        self.client.login(username="owner", password="testpass123")
        payload = {
            "results": [
                {
                    "round": 1,
                    "winner": self.entrant1.pk,
                    "winner_score": 450,
                    "loser": self.entrant2.pk,
                    "loser_score": 380,
                    "winner_started": True,
                },
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(self.division.result_slips.count(), 1)
        slip = self.division.result_slips.first()
        self.assertEqual(slip.winner_score, 450)

    def test_post_replaces_existing_results(self):
        self._make_pairing(2, self.entrant1, self.entrant2)
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.entrant1,
            winner_score=400, loser=self.entrant2, loser_score=350, winner_started=True,
        )
        self.client.login(username="owner", password="testpass123")
        payload = {
            "results": [
                {
                    "round": 2,
                    "winner": self.entrant2.pk,
                    "winner_score": 500,
                    "loser": self.entrant1.pk,
                    "loser_score": 400,
                    "winner_started": False,
                },
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.result_slips.count(), 1)
        slip = self.division.result_slips.first()
        self.assertEqual(slip.round, 2)
        self.assertEqual(slip.winner, self.entrant2)

    def test_post_rejects_result_without_pairing(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "results": [
                {
                    "round": 1,
                    "winner": self.entrant1.pk,
                    "winner_score": 450,
                    "loser": self.entrant2.pk,
                    "loser_score": 380,
                    "winner_started": True,
                },
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertTrue(any("no pairing" in e for e in body["errors"]))
        self.assertEqual(self.division.result_slips.count(), 0)

    def test_post_same_winner_loser_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "results": [
                {
                    "round": 1,
                    "winner": self.entrant1.pk,
                    "winner_score": 450,
                    "loser": self.entrant1.pk,
                    "loser_score": 380,
                    "winner_started": True,
                },
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("errors", body)
        self.assertEqual(self.division.result_slips.count(), 0)

    def test_post_missing_fields_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "results": [
                {
                    "round": 1,
                    "winner": self.entrant1.pk,
                },
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("errors", body)
        self.assertEqual(self.division.result_slips.count(), 0)


@tag("slow")
class TestDivisionVisibilityTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.test_division = Division.objects.create(
            name="Test Div", tournament=self.tournament, is_test=True
        )

    def test_test_division_hidden_on_tournament_detail_for_non_editor(self):
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertContains(response, "Open")
        self.assertNotContains(response, "Test Div")

    def test_test_division_shown_on_tournament_detail_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertContains(response, "Open")
        self.assertContains(response, "Test Div")

    def test_test_division_detail_404_for_non_editor(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.test_division.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_test_division_detail_works_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.test_division.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_regular_division_visible_to_non_editor(self):
        response = self.client.get(
            reverse("division_detail", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)


@tag("slow")
class DivisionFixedTablesEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_fixed_tables", kwargs={"pk": self.division.pk})

    def test_editor_can_access(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_returns_entrant_values(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        entrant_values = json.loads(response.context["entrant_values_json"])
        ids = {e["id"] for e in entrant_values}
        self.assertIn(self.entrant1.pk, ids)
        self.assertIn(self.entrant2.pk, ids)

    def test_get_round_values_includes_all(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        round_values = json.loads(response.context["round_values_json"])
        values = [r["value"] for r in round_values]
        self.assertIn(-1, values)
        self.assertIn(1, values)
        all_entry = next(r for r in round_values if r["value"] == -1)
        self.assertEqual(all_entry["label"], "All")

    def test_get_returns_existing_fixed_tables(self):
        FixedTable.objects.create(
            division=self.division, round_number=1,
            entrant=self.entrant1, table_number=2,
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        fixed_tables = json.loads(response.context["fixed_tables_json"])
        self.assertEqual(len(fixed_tables), 1)
        self.assertEqual(fixed_tables[0]["entrant"], self.entrant1.pk)
        self.assertEqual(fixed_tables[0]["table_number"], 2)

    def test_post_saves_fixed_table(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"tables": [{"round_number": 1, "entrant": self.entrant1.pk, "table_number": 2}]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.division.fixed_tables.count(), 1)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.entrant, self.entrant1)
        self.assertEqual(ft.table_number, 2)

    def test_post_all_sentinel_round(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"tables": [{"round_number": -1, "entrant": self.entrant1.pk, "table_number": 1}]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.round_number, -1)

    def test_post_replaces_existing(self):
        FixedTable.objects.create(
            division=self.division, round_number=1, entrant=self.entrant1, table_number=1,
        )
        self.client.login(username="owner", password="testpass123")
        payload = {"tables": [{"round_number": 2, "entrant": self.entrant2.pk, "table_number": 3}]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.fixed_tables.count(), 1)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.entrant, self.entrant2)

    def test_post_missing_fields_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"tables": [{"round_number": 1, "entrant": self.entrant1.pk}]}  # missing table_number
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_invalid_entrant_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"tables": [{"round_number": 1, "entrant": 99999, "table_number": 1}]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_duplicate_entrant_per_round_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "tables": [
                {"round_number": 1, "entrant": self.entrant1.pk, "table_number": 1},
                {"round_number": 1, "entrant": self.entrant1.pk, "table_number": 2},
            ]
        }
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_same_entrant_different_rounds_is_valid(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "tables": [
                {"round_number": 1, "entrant": self.entrant1.pk, "table_number": 1},
                {"round_number": 2, "entrant": self.entrant1.pk, "table_number": 2},
            ]
        }
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.division.fixed_tables.count(), 2)


@tag("slow")
class DivisionBoardTableMapEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_board_tables", kwargs={"pk": self.division.pk})

    def test_editor_can_access(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_returns_existing_map(self):
        settings_obj = DivisionSettings.objects.create(
            division=self.division,
            board_table_map=[{"board": 1, "table": 1}, {"board": 2, "table": 1}],
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        rows = json.loads(response.context["board_table_map_json"])
        self.assertEqual(rows, [{"board": 1, "table": 1}, {"board": 2, "table": 1}])
        del settings_obj

    def test_get_default_board_count_derived_from_entrants(self):
        # setUpTournament creates 2 entrants -> 1 board (ceil(2/2))
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.context["default_board_count"], 1)

    def test_post_saves_map(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [
            {"board": 1, "table": 1},
            {"board": 2, "table": 1},
            {"board": 3, "table": 2},
        ]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.division.refresh_from_db()
        self.assertEqual(
            self.division.settings.board_table_map,
            [{"board": 1, "table": 1}, {"board": 2, "table": 1}, {"board": 3, "table": 2}],
        )

    def test_post_sorts_by_board(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [
            {"board": 3, "table": 2},
            {"board": 1, "table": 1},
        ]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        boards = [r["board"] for r in self.division.settings.board_table_map]
        self.assertEqual(boards, [1, 3])

    def test_post_rejects_duplicate_boards(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [
            {"board": 1, "table": 1},
            {"board": 1, "table": 2},
        ]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_post_rejects_non_positive(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [{"board": 0, "table": 1}]}
        response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)


@tag("slow")
class SimulateMatchViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.test_division = Division.objects.create(
            name="Test Div", tournament=self.tournament, is_test=True
        )
        self.test_entrant1 = Entrant.objects.create(
            division=self.test_division, player=self.player1, number=1
        )
        self.test_entrant2 = Entrant.objects.create(
            division=self.test_division, player=self.player2, number=2
        )
        self.url = reverse("simulate_match", kwargs={"pk": self.test_division.pk})

    def test_creates_result_slip(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            self.url,
            json.dumps({"round": 1, "first": "Alice", "second": "Bob"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.test_division.result_slips.count(), 1)
        slip = self.test_division.result_slips.first()
        self.assertEqual(slip.round, 1)
        self.assertIn(slip.winner, [self.test_entrant1, self.test_entrant2])
        self.assertIn(slip.loser, [self.test_entrant1, self.test_entrant2])
        self.assertNotEqual(slip.winner, slip.loser)

    def test_forbidden_for_non_test_division(self):
        self.client.login(username="owner", password="testpass123")
        url = reverse("simulate_match", kwargs={"pk": self.division.pk})
        response = self.client.post(
            url,
            json.dumps({"round": 1, "first": "Alice", "second": "Bob"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.division.result_slips.count(), 0)

    def test_forbidden_for_non_editor(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            self.url,
            json.dumps({"round": 1, "first": "Alice", "second": "Bob"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.test_division.result_slips.count(), 0)


@tag("slow")
class DivisionEntrantsEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.division.entrants.all().delete()
        self.player3 = Player.objects.create(name="Charlie", player_number="003", rating=1400)
        self.url = reverse("division_entrants_edit", kwargs={"pk": self.division.pk})

    def test_editor_can_access(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_returns_json_context(self):
        Entrant.objects.create(division=self.division, player=self.player1, number=1)
        Entrant.objects.create(division=self.division, player=self.player2, number=2)
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        entrants = json.loads(response.context["entrants_json"])
        players = json.loads(response.context["players_json"])
        self.assertEqual(len(entrants), 2)
        self.assertEqual(entrants[0]["player"], self.player1.pk)
        self.assertEqual(entrants[1]["player"], self.player2.pk)
        # players_json should include all players in the DB
        player_ids = {p["id"] for p in players}
        self.assertIn(self.player1.pk, player_ids)
        self.assertIn(self.player2.pk, player_ids)
        self.assertIn(self.player3.pk, player_ids)

    def test_post_saves_entrants(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "entrants": [
                {"number": 1, "player": self.player1.pk},
                {"number": 2, "player": self.player2.pk},
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.division.entrants.count(), 2)
        self.assertEqual(self.division.entrants.get(number=1).player, self.player1)

    def test_post_replaces_existing_entrants(self):
        Entrant.objects.create(division=self.division, player=self.player1, number=1)
        self.client.login(username="owner", password="testpass123")
        payload = {
            "entrants": [
                {"number": 1, "player": self.player2.pk},
                {"number": 2, "player": self.player3.pk},
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.entrants.count(), 2)
        self.assertEqual(self.division.entrants.get(number=1).player, self.player2)

    def test_post_duplicate_player_returns_errors(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "entrants": [
                {"number": 1, "player": self.player1.pk},
                {"number": 2, "player": self.player1.pk},
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.entrants.count(), 0)

    def test_post_missing_fields_returns_errors(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "entrants": [
                {"number": 1},
            ]
        }
        response = self.client.post(
            self.url,
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.entrants.count(), 0)


