import json
from datetime import date

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    FixedPairing,
    FixedTable,
    Pairing,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.views import edit_key
from editgrid.models import EditPresence, EditVersion
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
    target.player1 = Player.objects.create(
        name="Alice", player_number="001", rating=1600
    )
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
            reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        edit_url = reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug})
        self.assertContains(response, edit_url)

    def test_no_edit_link_for_anonymous(self):
        response = self.client.get(
            reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        edit_url = reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug})
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
            response, reverse("tournament_detail", kwargs={"tournament_slug": tournament.slug})
        )
        self.assertEqual(tournament.owner, self.owner)


@tag("slow")
class DivisionCreateDeleteViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)

    def test_create_division(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_create", kwargs={"tournament_slug": self.tournament.slug}),
            {"name": "Open", "is_test": "0"},
        )
        self.assertTrue(
            self.tournament.divisions.filter(name="Open", is_test=False).exists()
        )

    def test_create_test_division(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_create", kwargs={"tournament_slug": self.tournament.slug}),
            {"name": "Sandbox", "is_test": "1"},
        )
        self.assertTrue(
            self.tournament.divisions.filter(name="Sandbox", is_test=True).exists()
        )

    def test_create_division_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_create", kwargs={"tournament_slug": self.tournament.slug}),
            {"name": "Novice", "is_test": "0"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.tournament.divisions.filter(name="Novice").exists())

    def test_create_redirects_to_tournament_detail(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_create", kwargs={"tournament_slug": self.tournament.slug}),
            {"name": "Open", "is_test": "0"},
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )

    def test_delete_confirmation_page_renders(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_delete", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.division.name)
        # Division must not be deleted by the GET.
        self.assertTrue(Division.objects.filter(pk=self.division.pk).exists())

    def test_delete_division_soft_deletes(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(reverse("division_delete", kwargs=self.division.slug_kwargs()))
        self.assertFalse(Division.objects.filter(pk=self.division.pk).exists())
        self.assertTrue(
            Division.all_objects.filter(pk=self.division.pk, is_deleted=True).exists()
        )

    def test_delete_division_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_delete", kwargs=self.division.slug_kwargs()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Division.objects.filter(pk=self.division.pk).exists())

    def test_delete_redirects_to_tournament_detail(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_delete", kwargs=self.division.slug_kwargs()),
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )

    def test_restore_division(self):
        self.division.soft_delete()
        self.client.login(username="owner", password="testpass123")
        self.client.post(reverse("division_restore", kwargs=self.division.slug_kwargs()))
        self.assertTrue(
            Division.objects.filter(pk=self.division.pk, is_deleted=False).exists()
        )

    def test_restore_division_non_editor_forbidden(self):
        self.division.soft_delete()
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_restore", kwargs=self.division.slug_kwargs()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            Division.all_objects.filter(pk=self.division.pk, is_deleted=True).exists()
        )


class DivisionRenameViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)

    def test_rename_division(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            {"name": "Championship"},
        )
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Championship")

    def test_rename_strips_whitespace(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            {"name": "  Trimmed  "},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Trimmed")

    def test_rename_empty_name_rejected(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            {"name": "   "},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Open")

    def test_rename_duplicate_name_rejected(self):
        Division.objects.create(name="Expert", tournament=self.tournament)
        self.client.login(username="owner", password="testpass123")
        self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            {"name": "Expert"},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Open")

    def test_rename_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            {"name": "Hacked"},
        )
        self.assertEqual(response.status_code, 403)
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Open")

    def test_rename_duplicate_datastar_surfaces_error(self):
        Division.objects.create(name="Expert", tournament=self.tournament)
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            data=json.dumps({"name": "Expert"}),
            content_type="application/json",
            headers={"datastar-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Open")
        body = b"".join(response.streaming_content).decode()
        self.assertIn("already exists", body)
        self.assertIn('class="error"', body)

    def test_rename_datastar_returns_management_fragment(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("division_rename", kwargs=self.division.slug_kwargs()),
            data=json.dumps({"name": "Masters"}),
            content_type="application/json",
            headers={"datastar-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.division.refresh_from_db()
        self.assertEqual(self.division.name, "Masters")
        body = b"".join(response.streaming_content).decode()
        # The swapped-in fragment shows the new name and resets the edit signals.
        self.assertIn("division-management", body)
        self.assertIn("Masters", body)
        self.assertIn("renamingPk", body)


@tag("slow")
class TournamentUpdateViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.editor = User.objects.create_user(
            username="editor", password="testpass123"
        )
        self.tournament.editors.add(self.editor)

    def test_owner_can_edit(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 200)

    def test_editor_can_edit(self):
        self.client.login(username="editor", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 200)

    def test_other_user_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 403)

    def test_update_tournament(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("tournament_edit", kwargs={"tournament_slug": self.tournament.slug}),
            {
                "name": "Updated Tournament",
                "location": "Updated Location",
                "start_date": "2026-06-01",
                "editor_usernames": "",
            },
        )
        # Renaming re-syncs the slug, so the redirect targets the new slug.
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.slug, "updated-tournament")
        self.assertRedirects(
            response, reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(self.tournament.name, "Updated Tournament")


@tag("slow")
class TournamentDeleteViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.editor = User.objects.create_user(
            username="editor", password="testpass123"
        )
        self.tournament.editors.add(self.editor)

    def test_owner_can_delete(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_delete", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/tournament_confirm_delete.html")

    def test_editor_cannot_delete(self):
        self.client.login(username="editor", password="testpass123")
        response = self.client.get(
            reverse("tournament_delete", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_tournament(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("tournament_delete", kwargs={"tournament_slug": self.tournament.slug})
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
        f = SimpleUploadedFile(
            "import.csv", csv_content.encode("utf-8"), content_type="text/csv"
        )
        return self.client.post(
            reverse("bulk_import_entrants", kwargs=self.division.slug_kwargs()),
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
        other_player = Player.objects.create(
            name="Eve", player_number="003", rating=1200
        )
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
            division=cls.division,
            round=1,
            status=RoundPairings.PUBLISHED,
        )
        cls.pairing = Pairing.objects.create(
            division=cls.division,
            round=1,
            round_pairings=cls.rp,
            first=cls.entrant1,
            second=cls.entrant2,
            table=1,
        )

    def test_create_result_slip(self):
        response = self.client.post(
            reverse("resultslip_create", kwargs=self.division.slug_kwargs()),
            {
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant1.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
                "verified_by_opponent": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        # The saved result is confirmed with an Edit button rather than a
        # Save-then-Done confirmation step.
        self.assertContains(response, "Saved:")
        self.assertContains(response, "Edit")
        self.assertEqual(self.division.result_slips.count(), 1)
        rs = self.division.result_slips.first()
        self.assertEqual(rs.pairing, self.pairing)

    def test_edit_existing_result_slip(self):
        rs = ResultSlip.objects.create(
            division=self.division,
            round=1,
            pairing=self.pairing,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        response = self.client.post(
            reverse(
                "resultslip_edit",
                kwargs={**self.division.slug_kwargs(), "result_pk": rs.pk},
            ),
            {
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": self.entrant2.pk,
                "winner_score": 500,
                "loser_score": 400,
                "winner_started": False,
                "verified_by_opponent": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        # No new slip is created; the existing one is updated in place.
        self.assertEqual(self.division.result_slips.count(), 1)
        rs.refresh_from_db()
        self.assertEqual(rs.winner, self.entrant2)
        self.assertEqual(rs.winner_score, 500)
        self.assertEqual(rs.loser, self.entrant1)
        self.assertEqual(rs.loser_score, 400)

    def test_edit_form_prefills_existing_result(self):
        rs = ResultSlip.objects.create(
            division=self.division,
            round=1,
            pairing=self.pairing,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        response = self.client.get(
            reverse(
                "resultslip_edit",
                kwargs={**self.division.slug_kwargs(), "result_pk": rs.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        # The pairing whose result is being edited is offered even though it
        # already has a result, so the form can render its current selection.
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_form_shows_pairing_options(self):
        response = self.client.get(
            reverse("resultslip_create", kwargs=self.division.slug_kwargs())
        )
        self.assertContains(response, "Alice")
        self.assertContains(response, "Bob")

    def test_prefill_pairing_preselects_round_and_pairing(self):
        # The published-pairings "Submit results" link deep-links a match via
        # ?pairing=<pk>; the form opens with that round and pairing selected.
        response = self.client.get(
            reverse("resultslip_create", kwargs=self.division.slug_kwargs())
            + f"?pairing={self.pairing.pk}"
        )
        # Round select: round 1 chosen.
        self.assertContains(response, '<option value="1" selected>1</option>', html=True)
        # Pairing select: the linked pairing chosen. Anchored on the label so it
        # can't be satisfied by the round option (whose value happens to coincide
        # with the pairing pk in this fixture).
        self.assertContains(
            response,
            f'<option value="{self.pairing.pk}" '
            'data-show="!$round || Number($round) === 1" selected>Alice vs. Bob</option>',
            html=True,
        )

    def test_invalid_winner_not_in_pairing(self):
        other_player = Player.objects.create(
            name="Charlie", player_number="003", rating=1400
        )
        other_entrant = Entrant.objects.create(
            division=self.division, player=other_player, number=3
        )
        response = self.client.post(
            reverse("resultslip_create", kwargs=self.division.slug_kwargs()),
            {
                "round": 1,
                "pairing": self.pairing.pk,
                "winner": other_entrant.pk,
                "winner_score": 450,
                "loser_score": 380,
                "winner_started": True,
                "verified_by_opponent": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Winner must be one of the players in the pairing"
        )


@tag("slow")
class DivisionDetailLatestResultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_shows_only_max_round_results(self):
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=400,
            loser=self.entrant2,
            loser_score=350,
            winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division,
            round=2,
            winner=self.entrant2,
            winner_score=420,
            loser=self.entrant1,
            loser_score=390,
            winner_started=False,
        )
        response = self.client.get(
            reverse("division_detail", kwargs=self.division.slug_kwargs())
        )
        # Round 2 scores should appear, round 1 scores should not
        self.assertContains(response, "420")
        self.assertNotContains(response, "400-350")

    def test_settings_link_shown_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_detail", kwargs=self.division.slug_kwargs())
        )
        self.assertContains(
            response, reverse("division_settings", kwargs=self.division.slug_kwargs())
        )

    def test_settings_link_hidden_for_anonymous(self):
        response = self.client.get(
            reverse("division_detail", kwargs=self.division.slug_kwargs())
        )
        self.assertNotContains(response, "Settings")


@tag("slow")
class DivisionStandingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_standings_with_results(self):
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        response = self.client.get(
            reverse("division_standings", kwargs=self.division.slug_kwargs())
        )
        # Names are shown with the entrant's seed number.
        self.assertContains(response, "Alice (#1)")
        self.assertContains(response, "Bob (#2)")

    def test_standings_for_specific_round(self):
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division,
            round=2,
            winner=self.entrant2,
            winner_score=420,
            loser=self.entrant1,
            loser_score=390,
            winner_started=False,
        )
        response = self.client.get(
            reverse("division_standings_round", kwargs={**self.division.slug_kwargs(), "round": 1})
        )
        self.assertEqual(response.status_code, 200)
        # After round 1 only, Alice has 1 win
        self.assertContains(response, "After round 1")

    def test_round_links(self):
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        ResultSlip.objects.create(
            division=self.division,
            round=2,
            winner=self.entrant2,
            winner_score=420,
            loser=self.entrant1,
            loser_score=390,
            winner_started=False,
        )
        response = self.client.get(
            reverse("division_standings", kwargs=self.division.slug_kwargs())
        )
        self.assertContains(
            response,
            reverse("division_standings_round", kwargs={**self.division.slug_kwargs(), "round": 1}),
        )


@tag("slow")
class DivisionAllResultsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_edit_results_button_shown_to_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_all_results", kwargs=self.division.slug_kwargs())
        )
        edit_url = reverse("division_edit_results", kwargs=self.division.slug_kwargs())
        self.assertContains(
            response,
            f'<a href="{edit_url}" class="btn-secondary">Edit Results</a>',
            html=True,
        )

    def test_edit_results_button_hidden_from_anonymous(self):
        response = self.client.get(
            reverse("division_all_results", kwargs=self.division.slug_kwargs())
        )
        edit_url = reverse("division_edit_results", kwargs=self.division.slug_kwargs())
        self.assertNotContains(response, f'href="{edit_url}"')


@tag("slow")
class DivisionSettingsEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_settings", kwargs=self.division.slug_kwargs())

    def test_editor_sees_placeholder(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tournaments/division_settings_edit.html")

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.get(self.url).status_code, 403)


class DivisionRoundPairingsEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_round_pairings", kwargs=self.division.slug_kwargs())

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_get_exposes_blocks_defaults_and_strategies(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("default_rounds_json", response.context)
        self.assertIn("KotH", json.loads(response.context["strategy_types_json"]))

    def test_get_backfills_blocks_from_existing_schedule(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[
                {"round": 1, "pairing": "Swiss", "start_round": 0},
                {"round": 2, "pairing": "Swiss", "start_round": 1},
                {"round": 3, "pairing": "KotH", "start_round": 2},
            ],
        )
        self.client.login(username="owner", password="testpass123")
        blocks = json.loads(self.client.get(self.url).context["blocks_json"])
        self.assertEqual(
            blocks,
            [
                {"pairing": "Swiss", "rounds": 2, "pair_from": 1},
                {"pairing": "KotH", "rounds": 1, "pair_from": 1},
            ],
        )

    def test_post_saves_blocks_and_derives_round_pairings(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "blocks": [
                {"pairing": "KotH", "rounds": 3, "pair_from": 1},
                {"pairing": "Swiss", "rounds": 2, "pair_from": 2},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        s = DivisionSettings.objects.get(division=self.division)
        self.assertEqual(s.pairing_blocks, payload["blocks"])
        rounds = [
            (rp["round"], rp["pairing"], rp["start_round"]) for rp in s.round_pairings
        ]
        self.assertEqual(
            rounds,
            [
                (1, "KotH", 0),
                (2, "KotH", 1),
                (3, "KotH", 2),  # sliding: round - 1
                (4, "Swiss", 2),
                (5, "Swiss", 3),  # sliding: round - 2
            ],
        )

    def test_post_invalid_pairing_rejected(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"blocks": [{"pairing": "Nope", "rounds": 2, "pair_from": 1}]}
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())

    def test_preview_expands_without_saving(self):
        self.client.login(username="owner", password="testpass123")
        url = reverse("division_round_pairings_preview", kwargs=self.division.slug_kwargs())
        payload = {
            "blocks": [{"pairing": "Quads_Clustered", "rounds": 3, "pair_from": 1}]
        }
        response = self.client.post(
            url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        # Quads pair off one fixed snapshot: start_round = blockStart - 1 = 0 for all.
        self.assertEqual([r["start_round"] for r in response.json()["rows"]], [0, 0, 0])
        self.assertFalse(
            DivisionSettings.objects.filter(division=self.division).exists()
        )


@tag("slow")
class DivisionPairingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def setUp(self):
        # The pairings tab is editor-only.
        self.client.login(username="owner", password="testpass123")

    def test_no_pairings_configured_shows_message(self):
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertContains(response, "No round pairings configured")

    def test_with_pairings_in_db_shows_pairings(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        Pairing.objects.create(
            division=self.division,
            round=1,
            first=self.entrant1,
            second=self.entrant2,
            repeats=0,
        )
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
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
            division=self.division,
            round=1,
            first=self.entrant1,
            second=self.entrant2,
            repeats=0,
        )
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        # Selected round should be round 1 (pairable)
        self.assertEqual(response.context["selected_round"], 1)
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 1)

    def test_pairings_autogenerated_for_pairable_round(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        self.client.login(username="owner", password="testpass123")
        # Viewing the pairings tab auto-generates the pairable round's pairings.
        self.client.get(reverse("division_pair_rounds", kwargs=self.division.slug_kwargs()))
        self.assertEqual(Pairing.objects.filter(division=self.division).count(), 1)

    def test_pairings_not_autogenerated_for_non_editor(self):
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )
        self.client.logout()  # the pairings tab is editor-only
        self.client.get(reverse("division_pair_rounds", kwargs=self.division.slug_kwargs()))
        self.assertEqual(Pairing.objects.filter(division=self.division).count(), 0)

    def test_pairings_link_on_division_detail(self):
        response = self.client.get(
            reverse("division_detail", kwargs=self.division.slug_kwargs())
        )
        self.assertContains(
            response,
            reverse("division_pairings", kwargs=self.division.slug_kwargs()),
        )


@tag("slow")
class DivisionPairingsRoundContentTests(TestCase):
    """Verify the pairings view returns the full pairings table for each round,
    including unplayed matches alongside played ones for in-progress rounds."""

    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.player3 = Player.objects.create(
            name="Carol", player_number="003", rating=1400
        )
        cls.player4 = Player.objects.create(
            name="Dave", player_number="004", rating=1300
        )
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

    def setUp(self):
        # The pairings tab is editor-only.
        self.client.login(username="owner", password="testpass123")

    def _create_round(self, round_num, status, pairs):
        """Create a RoundPairings with two Pairing rows for the given round."""
        rp = RoundPairings.objects.create(
            division=self.division,
            round=round_num,
            status=status,
        )
        pairings = []
        for table, (first, second) in enumerate(pairs, start=1):
            pairings.append(
                Pairing.objects.create(
                    division=self.division,
                    round=round_num,
                    round_pairings=rp,
                    first=first,
                    second=second,
                    table=table,
                )
            )
        return rp, pairings

    def _create_slip(
        self, round_num, pairing, winner, loser, winner_score, loser_score
    ):
        return ResultSlip.objects.create(
            division=self.division,
            round=round_num,
            pairing=pairing,
            winner=winner,
            winner_score=winner_score,
            loser=loser,
            loser_score=loser_score,
            winner_started=True,
        )

    def test_in_progress_round_shows_played_and_unplayed_pairings(self):
        _, pairings = self._create_round(
            1,
            RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
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
            1,
            RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, pairings[1], self.entrant3, self.entrant4, 500, 400)

        # Navigate directly to round 1 so we don't get auto-routed to a pairable later round.
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={**self.division.slug_kwargs(), "round": 1})
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
            1,
            RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        # Navigate directly to round 1: round 2 is now pairable (its prereq
        # round 1 has lifecycle status FINISHED), so default selection would
        # land on round 2 instead.
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={**self.division.slug_kwargs(), "round": 1})
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
            division=self.division,
            round=1,
            pairing=None,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={**self.division.slug_kwargs(), "round": 1})
        )
        tabs_by_round = {t["round"]: t for t in response.context["round_tabs"]}
        self.assertEqual(tabs_by_round[1]["status"], "error_no_pairings")
        self.assertEqual(response.context["selected_status"], "error_no_pairings")

    def test_published_pairings_page_excludes_finished_rounds(self):
        _, r1_pairings = self._create_round(
            1,
            RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, r1_pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, r1_pairings[1], self.entrant3, self.entrant4, 500, 400)
        self._create_round(
            2,
            RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant3), (self.entrant2, self.entrant4)],
        )

        response = self.client.get(
            reverse("published_pairings", kwargs=self.division.slug_kwargs())
        )
        rounds_shown = [r for r, _ in response.context["pairings"]]
        self.assertEqual(rounds_shown, [2])

    def test_published_round_shows_published_tab_status(self):
        self._create_round(
            1,
            RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        tabs_by_round = {t["round"]: t for t in response.context["round_tabs"]}
        self.assertEqual(tabs_by_round[1]["status"], "published")
        self.assertEqual(response.context["selected_status"], "published")

    def test_public_pairings_tab_visible_to_non_editor(self):
        # The "Pairings" tab shows published pairings in-nav to everyone, unlike
        # the editor-only "Pair rounds" tab.
        self._create_round(
            1, RoundPairings.PUBLISHED, [(self.entrant1, self.entrant2)]
        )
        self.client.logout()
        response = self.client.get(
            reverse("division_pairings", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "pairings")
        rounds_shown = [r for r, _ in response.context["pairings"]]
        self.assertEqual(rounds_shown, [1])

    def test_published_round_not_regenerated_and_future_round_left_alone(self):
        _, pairings = self._create_round(
            1,
            RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        original_pks = sorted(p.pk for p in pairings)
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        # The manual Generate button is gone.
        self.assertNotContains(response, "Generate Pairings")
        # Round 1 is published (not pairable) so auto-generation leaves it intact;
        # round 2 waits on round 1's results, so it is not generated either.
        self.assertEqual(
            sorted(p.pk for p in self.division.pairings.filter(round=1)),
            original_pks,
        )
        self.assertEqual(self.division.pairings.filter(round=2).count(), 0)

    def test_add_fixed_pairing_redrafts_published_round(self):
        _, pairings = self._create_round(
            1,
            RoundPairings.PUBLISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        original_pks = sorted(p.pk for p in pairings)

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("add_fixed_pairing", kwargs=self.division.slug_kwargs()),
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
            1,
            RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("add_fixed_pairing", kwargs=self.division.slug_kwargs()),
            {"round": 1, "entrant1": self.entrant3.pk, "entrant2": self.entrant4.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.division.fixed_pairings.exists())

    def test_remove_fixed_pairing_blocked_when_affected_round_has_results(self):
        _, pairings = self._create_round(
            1,
            RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        fp = FixedPairing.objects.create(
            division=self.division,
            round_number=1,
            entrant1=self.entrant3,
            entrant2=self.entrant4,
        )

        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            reverse("remove_fixed_pairings", kwargs=self.division.slug_kwargs()),
            {"keep": []},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.division.fixed_pairings.filter(pk=fp.pk).exists())

    def test_has_published_rounds_false_when_only_finished(self):
        _, pairings = self._create_round(
            1,
            RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, pairings[1], self.entrant3, self.entrant4, 500, 400)
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertFalse(response.context["has_published_rounds"])

    def test_in_progress_round_selected_when_earlier_round_finished(self):
        _, r1_pairings = self._create_round(
            1,
            RoundPairings.FINISHED,
            [(self.entrant1, self.entrant2), (self.entrant3, self.entrant4)],
        )
        self._create_slip(1, r1_pairings[0], self.entrant1, self.entrant2, 450, 380)
        self._create_slip(1, r1_pairings[1], self.entrant3, self.entrant4, 500, 400)
        _, r2_pairings = self._create_round(
            2,
            RoundPairings.IN_PROGRESS,
            [(self.entrant1, self.entrant3), (self.entrant2, self.entrant4)],
        )
        self._create_slip(2, r2_pairings[0], self.entrant1, self.entrant3, 420, 410)

        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.context["selected_round"], 2)
        self.assertEqual(response.context["selected_status"], "in_progress")
        round_pairings = response.context["round_pairings"]
        self.assertEqual(len(round_pairings), 2)
        by_table = {e.pairing.table: e for e in round_pairings}
        self.assertEqual(by_table[1].result, "420 - 410")
        self.assertEqual(by_table[2].result, "")


DATASTAR_HEADERS = {"datastar-request": "true"}


class InlineFixedPairingTests(TestCase):
    """The fixed-pairings section embedded in a pairable round of the pairings tab."""

    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.player3 = Player.objects.create(
            name="Carol", player_number="003", rating=1400
        )
        cls.player4 = Player.objects.create(
            name="Dave", player_number="004", rating=1300
        )
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

    def _datastar_post(self, name, payload):
        return self.client.post(
            reverse(name, kwargs=self.division.slug_kwargs()),
            data=json.dumps(payload),
            content_type="application/json",
            headers=DATASTAR_HEADERS,
        )

    def test_section_shown_for_editor_on_pairable_round(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.context["selected_status"], "pairable")
        self.assertContains(response, "Add fixed pairing")

    def test_pairings_page_forbidden_for_non_editor(self):
        # The whole pairings tab is editor-only.
        self.client.login(username="other", password="testpass123")
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 403)

    def test_datastar_add_creates_pairing_and_renders_it(self):
        self.client.login(username="owner", password="testpass123")
        response = self._datastar_post(
            "add_fixed_pairing",
            {"round": 1, "entrant1": self.entrant1.pk, "entrant2": self.entrant3.pk},
        )
        self.assertEqual(response.status_code, 200)
        fp = self.division.fixed_pairings.get(round_number=1)
        self.assertEqual(
            {fp.entrant1_id, fp.entrant2_id}, {self.entrant1.pk, self.entrant3.pk}
        )
        self.assertContains(response, "Alice vs. Carol")
        # The round was regenerated with the fix honoured.
        paired = self.division.pairings.filter(round=1).filter(
            first__in=[self.entrant1, self.entrant3],
            second__in=[self.entrant1, self.entrant3],
        )
        self.assertEqual(paired.count(), 1)

    def test_datastar_add_duplicate_player_renders_error(self):
        self.client.login(username="owner", password="testpass123")
        FixedPairing.objects.create(
            division=self.division,
            round_number=1,
            entrant1=self.entrant1,
            entrant2=self.entrant2,
        )
        response = self._datastar_post(
            "add_fixed_pairing",
            {"round": 1, "entrant1": self.entrant1.pk, "entrant2": self.entrant3.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already have a fixed pairing")
        self.assertEqual(self.division.fixed_pairings.filter(round_number=1).count(), 1)

    def test_datastar_remove_single_pairing(self):
        self.client.login(username="owner", password="testpass123")
        fp = FixedPairing.objects.create(
            division=self.division,
            round_number=1,
            entrant1=self.entrant1,
            entrant2=self.entrant3,
        )
        response = self._datastar_post(
            "remove_fixed_pairing", {"fp_id": fp.pk, "round": 1}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.division.fixed_pairings.filter(pk=fp.pk).exists())

    def test_datastar_remove_guarded_when_round_has_results(self):
        # A round with results renders as in_progress (no inline delete button),
        # so this path is not reachable via the UI; the backend guard still
        # refuses the delete as defense-in-depth.
        self.client.login(username="owner", password="testpass123")
        rp = RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.IN_PROGRESS
        )
        pairing = Pairing.objects.create(
            division=self.division,
            round=1,
            round_pairings=rp,
            first=self.entrant1,
            second=self.entrant2,
            table=1,
        )
        ResultSlip.objects.create(
            division=self.division,
            round=1,
            pairing=pairing,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        fp = FixedPairing.objects.create(
            division=self.division,
            round_number=1,
            entrant1=self.entrant3,
            entrant2=self.entrant4,
        )
        response = self._datastar_post(
            "remove_fixed_pairing", {"fp_id": fp.pk, "round": 1}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.division.fixed_pairings.filter(pk=fp.pk).exists())

    def test_publish_buttons_shown_on_pairable_round(self):
        self.client.login(username="owner", password="testpass123")
        # Viewing the tab auto-generates draft pairings for the pairable round.
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.context["selected_status"], "pairable")
        self.assertContains(response, "Publish All")
        self.assertContains(response, "Publish round 1")

    def test_publish_round_publishes_only_that_round(self):
        self.client.login(username="owner", password="testpass123")
        self.client.get(reverse("division_pair_rounds", kwargs=self.division.slug_kwargs()))
        response = self._datastar_post("publish_round", {"round": 1})
        self.assertEqual(response.status_code, 200)
        rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)
        # Round 2 was not pairable, so nothing was published there.
        self.assertFalse(
            self.division.round_pairings_set.filter(
                round=2, status=RoundPairings.PUBLISHED
            ).exists()
        )

    def test_publish_all_publishes_every_draft(self):
        self.client.login(username="owner", password="testpass123")
        RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.DRAFT
        )
        RoundPairings.objects.create(
            division=self.division, round=2, status=RoundPairings.DRAFT
        )
        response = self._datastar_post("publish_pairings", {})
        self.assertEqual(response.status_code, 200)
        statuses = set(
            self.division.round_pairings_set.values_list("status", flat=True)
        )
        self.assertEqual(statuses, {RoundPairings.PUBLISHED})


@tag("slow")
class DivisionEditResultsViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_edit_results", kwargs=self.division.slug_kwargs())

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
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=450,
            loser=self.entrant2,
            loser_score=380,
            winner_started=True,
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        grid = response.context["grid"]
        results = grid.rows
        entrants = grid.lookups["entrants"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["round"], 1)
        self.assertEqual(results[0]["winner"], self.entrant1.pk)
        self.assertEqual(results[0]["winner_score"], 450)
        self.assertEqual(len(entrants), 2)
        labels = {e["label"] for e in entrants}
        self.assertEqual(labels, {"Alice", "Bob"})

    def _make_pairing(self, round_num, first, second, table=1):
        return Pairing.objects.create(
            division=self.division,
            round=round_num,
            first=first,
            second=second,
            table=table,
        )

    def test_post_saves_results(self):
        self._make_pairing(1, self.entrant1, self.entrant2)
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
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
            division=self.division,
            round=1,
            winner=self.entrant1,
            winner_score=400,
            loser=self.entrant2,
            loser_score=350,
            winner_started=True,
        )
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
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
            "rows": [
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
            "rows": [
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
            "rows": [
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
            reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertContains(response, "Open")
        self.assertNotContains(response, "Test Div")

    def test_test_division_shown_on_tournament_detail_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug})
        )
        self.assertContains(response, "Open")
        self.assertContains(response, "Test Div")

    def test_test_division_detail_404_for_non_editor(self):
        response = self.client.get(
            reverse("division_detail", kwargs=self.test_division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 404)

    def test_test_division_detail_works_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_detail", kwargs=self.test_division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 200)

    def test_regular_division_visible_to_non_editor(self):
        response = self.client.get(
            reverse("division_detail", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(response.status_code, 200)


@tag("slow")
class DivisionFixedTablesEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_fixed_tables", kwargs=self.division.slug_kwargs())

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
        entrant_values = response.context["grid"].lookups["entrantValues"]
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
        round_values = response.context["grid"].lookups["roundValues"]
        values = [r["id"] for r in round_values]
        self.assertIn(-1, values)
        self.assertIn(1, values)
        all_entry = next(r for r in round_values if r["id"] == -1)
        self.assertEqual(all_entry["label"], "All")

    def test_get_returns_existing_fixed_tables(self):
        FixedTable.objects.create(
            division=self.division,
            round_number=1,
            entrant=self.entrant1,
            table_label="S2",
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        fixed_tables = response.context["grid"].rows
        self.assertEqual(len(fixed_tables), 1)
        self.assertEqual(fixed_tables[0]["entrant"], self.entrant1.pk)
        self.assertEqual(fixed_tables[0]["table_label"], "S2")

    def test_post_saves_fixed_table(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"round_number": 1, "entrant": self.entrant1.pk, "table_label": "S2"}
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.division.fixed_tables.count(), 1)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.entrant, self.entrant1)
        self.assertEqual(ft.table_label, "S2")

    def test_post_all_sentinel_round(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"round_number": -1, "entrant": self.entrant1.pk, "table_label": "1"}
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.round_number, -1)

    def test_post_replaces_existing(self):
        FixedTable.objects.create(
            division=self.division,
            round_number=1,
            entrant=self.entrant1,
            table_label="1",
        )
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"round_number": 2, "entrant": self.entrant2.pk, "table_label": "3"}
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.fixed_tables.count(), 1)
        ft = self.division.fixed_tables.first()
        self.assertEqual(ft.entrant, self.entrant2)

    def test_post_missing_fields_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [{"round_number": 1, "entrant": self.entrant1.pk}]
        }  # missing table_label
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_invalid_entrant_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [{"round_number": 1, "entrant": 99999, "table_label": "1"}]}
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_duplicate_entrant_per_round_returns_error(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"round_number": 1, "entrant": self.entrant1.pk, "table_label": "1"},
                {"round_number": 1, "entrant": self.entrant1.pk, "table_label": "2"},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(self.division.fixed_tables.count(), 0)

    def test_post_same_entrant_different_rounds_is_valid(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"round_number": 1, "entrant": self.entrant1.pk, "table_label": "1"},
                {"round_number": 2, "entrant": self.entrant1.pk, "table_label": "2"},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.division.fixed_tables.count(), 2)


@tag("slow")
class DivisionBoardTableMapEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_board_tables", kwargs=self.division.slug_kwargs())

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
        rows = response.context["grid"].rows
        self.assertEqual(rows, [{"board": 1, "table": 1}, {"board": 2, "table": 1}])
        del settings_obj

    def test_get_default_board_count_derived_from_entrants(self):
        # setUpTournament creates 2 entrants -> 1 board (ceil(2/2))
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.context["default_board_count"], 1)

    def test_post_saves_map(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"board": 1, "table": 1, "label": "S1"},
                {"board": 2, "table": 2, "label": "1"},
                {"board": 3, "table": 2, "label": "1"},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.division.refresh_from_db()
        self.assertEqual(
            self.division.settings.board_table_map,
            [
                {"board": 1, "table": 1, "label": "S1"},
                {"board": 2, "table": 2, "label": "1"},
                {"board": 3, "table": 2, "label": "1"},
            ],
        )

    def test_post_defaults_missing_label_to_table(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [{"board": 1, "table": 5}]}
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.division.refresh_from_db()
        self.assertEqual(
            self.division.settings.board_table_map,
            [{"board": 1, "table": 5, "label": "5"}],
        )

    def test_post_sorts_by_board(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"board": 3, "table": 2},
                {"board": 1, "table": 1},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        boards = [r["board"] for r in self.division.settings.board_table_map]
        self.assertEqual(boards, [1, 3])

    def test_post_rejects_duplicate_boards(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
                {"board": 1, "table": 1},
                {"board": 1, "table": 2},
            ]
        }
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_post_rejects_non_positive(self):
        self.client.login(username="owner", password="testpass123")
        payload = {"rows": [{"board": 0, "table": 1}]}
        response = self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )
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
        self.url = reverse("simulate_match", kwargs=self.test_division.slug_kwargs())

    def _publish_round_one(self):
        rp = RoundPairings.objects.create(
            division=self.test_division,
            round=1,
            status=RoundPairings.PUBLISHED,
        )
        Pairing.objects.create(
            division=self.test_division,
            round=1,
            round_pairings=rp,
            first=self.test_entrant1,
            second=self.test_entrant2,
            table=1,
        )

    def test_creates_result_slip(self):
        self._publish_round_one()
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

    def test_rejected_when_round_has_no_pairings(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            self.url,
            json.dumps({"round": 1, "first": "Alice", "second": "Bob"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.test_division.result_slips.count(), 0)

    def test_rejected_when_round_is_draft(self):
        RoundPairings.objects.create(
            division=self.test_division,
            round=1,
            status=RoundPairings.DRAFT,
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(
            self.url,
            json.dumps({"round": 1, "first": "Alice", "second": "Bob"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.test_division.result_slips.count(), 0)

    def test_forbidden_for_non_test_division(self):
        self.client.login(username="owner", password="testpass123")
        url = reverse("simulate_match", kwargs=self.division.slug_kwargs())
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
class SimulateButtonVisibilityTests(TestCase):
    """The simulate buttons must only appear on published rounds of a test
    division. Simulating a pairable (draft) round would record results against
    pairings that can still be regenerated, leaving results with no pairing.
    """

    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.test_division = Division.objects.create(
            name="Test Div",
            tournament=cls.tournament,
            is_test=True,
        )
        cls.e1 = Entrant.objects.create(
            division=cls.test_division, player=cls.player1, number=1
        )
        cls.e2 = Entrant.objects.create(
            division=cls.test_division, player=cls.player2, number=2
        )
        DivisionSettings.objects.create(
            division=cls.test_division,
            round_pairings=[{"round": 1, "pairing": "KotH", "start_round": 0}],
        )

    def test_simulate_hidden_on_pairable_round(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("division_pair_rounds", kwargs=self.test_division.slug_kwargs())
        )
        self.assertEqual(response.context["selected_status"], "pairable")
        self.assertNotContains(response, ">simulate<")
        self.assertNotContains(response, "simulate all")

    def test_simulate_shown_on_published_round(self):
        rp = RoundPairings.objects.create(
            division=self.test_division,
            round=1,
            status=RoundPairings.PUBLISHED,
        )
        Pairing.objects.create(
            division=self.test_division,
            round=1,
            round_pairings=rp,
            first=self.e1,
            second=self.e2,
            table=1,
        )
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(
            reverse("round_pairings_tab", kwargs={**self.test_division.slug_kwargs(), "round": 1})
        )
        self.assertEqual(response.context["selected_status"], "published")
        self.assertContains(response, ">simulate<")
        self.assertContains(response, "simulate all")


@tag("slow")
class DivisionEntrantsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.url = reverse("division_entrants", kwargs=cls.division.slug_kwargs())
        cls.edit_url = reverse("division_entrants_edit", kwargs=cls.division.slug_kwargs())

    def test_edit_entrants_button_shown_for_editor(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertContains(response, self.edit_url)

    def test_edit_entrants_button_hidden_for_non_editor(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertNotContains(response, self.edit_url)


class DivisionEntrantsEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.division.entrants.all().delete()
        self.player3 = Player.objects.create(
            name="Charlie", player_number="003", rating=1400
        )
        self.url = reverse("division_entrants_edit", kwargs=self.division.slug_kwargs())

    def test_editor_can_access(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_returns_grid_context(self):
        Entrant.objects.create(division=self.division, player=self.player1, number=1)
        Entrant.objects.create(division=self.division, player=self.player2, number=2)
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        grid = response.context["grid"]
        self.assertEqual(len(grid.rows), 2)
        self.assertEqual(grid.rows[0]["player"], self.player1.pk)
        self.assertEqual(grid.rows[1]["player"], self.player2.pk)
        # The players lookup should include all players in the DB.
        player_ids = {p["id"] for p in grid.lookups["players"]}
        self.assertIn(self.player1.pk, player_ids)
        self.assertIn(self.player2.pk, player_ids)
        self.assertIn(self.player3.pk, player_ids)

    def test_post_saves_entrants(self):
        self.client.login(username="owner", password="testpass123")
        payload = {
            "rows": [
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
            "rows": [
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
            "rows": [
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
            "rows": [
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

    def _save(self, players, version=None):
        payload = {
            "rows": [{"number": i + 1, "player": p.pk} for i, p in enumerate(players)]
        }
        if version is not None:
            payload["_version"] = version
        return self.client.post(
            self.url, json.dumps(payload), content_type="application/json"
        )

    def test_get_exposes_current_edit_version(self):
        self.client.login(username="owner", password="testpass123")
        # Starts at 0 before any save, and the GET reflects each saved version.
        self.assertEqual(self.client.get(self.url).context["grid"].version, 0)
        self._save([self.player1], version=0)
        self.assertEqual(self.client.get(self.url).context["grid"].version, 1)

    def test_save_bumps_and_returns_version(self):
        self.client.login(username="owner", password="testpass123")
        first = self._save([self.player1], version=0)
        self.assertEqual(first.json()["version"], 1)
        # The client reuses the returned version for a consecutive save.
        second = self._save([self.player2], version=1)
        self.assertEqual(second.json()["version"], 2)

    def test_stale_version_is_rejected_as_conflict(self):
        self.client.login(username="owner", password="testpass123")
        self._save([self.player1], version=0)  # bumps to v1
        # A second editor still holding v0 must be rejected, not silently win.
        response = self._save([self.player2], version=0)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body["conflict"])
        # The losing save is discarded — player1 survives.
        self.assertEqual(self.division.entrants.count(), 1)
        self.assertEqual(self.division.entrants.get().player, self.player1)

    def test_missing_version_skips_check(self):
        # A payload without _version (older page) still saves, for compatibility.
        self.client.login(username="owner", password="testpass123")
        self._save([self.player1])  # establishes v1
        response = self._save([self.player2])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.entrants.get().player, self.player2)


class EditPresenceViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("edit_presence", kwargs={**self.division.slug_kwargs(), "scope": "entrants"})
        self.key = edit_key(self.division, "entrants")

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_unknown_scope_404(self):
        self.client.login(username="owner", password="testpass123")
        url = reverse("edit_presence", kwargs={**self.division.slug_kwargs(), "scope": "bogus"})
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_heartbeat_records_self_and_returns_other_editors(self):
        # Another editor is already present on this grid.
        EditPresence.objects.create(key=self.key, user=self.other)
        self.client.login(username="owner", password="testpass123")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["editors"], ["other"])
        # The heartbeat recorded the caller too.
        self.assertTrue(
            EditPresence.objects.filter(key=self.key, user=self.owner).exists()
        )

    def test_release_removes_presence(self):
        self.client.login(username="owner", password="testpass123")
        self.client.post(self.url)  # heartbeat in
        response = self.client.post(self.url, {"release": "1"})
        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            EditPresence.objects.filter(key=self.key, user=self.owner).exists()
        )

    def test_heartbeat_without_known_version_omits_staleness(self):
        self.client.login(username="owner", password="testpass123")
        self.assertNotIn("stale", self.client.post(self.url).json())

    def test_heartbeat_reports_not_stale_when_version_matches(self):
        EditVersion.objects.create(key=self.key, version=3)
        self.client.login(username="owner", password="testpass123")
        body = self.client.post(self.url + "?known_version=3").json()
        self.assertFalse(body["stale"])
        self.assertEqual(body["current_version"], 3)

    def test_heartbeat_reports_stale_when_version_moved_on(self):
        EditVersion.objects.create(key=self.key, version=3)
        self.client.login(username="owner", password="testpass123")
        body = self.client.post(self.url + "?known_version=2").json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["current_version"], 3)


class DivisionFixturesEditViewTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.url = reverse("division_fixtures", kwargs=self.division.slug_kwargs())

    def test_non_editor_forbidden(self):
        self.client.login(username="other", password="testpass123")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_get_renders_both_grids_with_own_save_urls(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        pairings = response.context["pairings_grid"]
        tables = response.context["tables_grid"]
        # Distinct grids, each pointing at its own existing save endpoint.
        self.assertEqual(pairings.dom_id, "fixed-pairings-table")
        self.assertEqual(tables.dom_id, "fixed-tables-table")
        self.assertEqual(
            pairings.save_url,
            reverse("division_fixed_pairings", kwargs=self.division.slug_kwargs()),
        )
        self.assertEqual(
            tables.save_url,
            reverse("division_fixed_tables", kwargs=self.division.slug_kwargs()),
        )

    def test_each_grid_saves_via_its_own_endpoint(self):
        # The combined page is GET-only; saves go to the per-grid endpoints.
        self.client.login(username="owner", password="testpass123")
        pairings_url = reverse("division_fixed_pairings", kwargs=self.division.slug_kwargs())
        payload = {
            "rows": [
                {
                    "round_number": 1,
                    "entrant1": self.entrant1.pk,
                    "entrant2": self.entrant2.pk,
                }
            ]
        }
        response = self.client.post(
            pairings_url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.division.fixed_pairings.count(), 1)


class SlugURLRedirectTests(TestCase):
    """Old (aliased) slugs 301 to the canonical URL after a rename; stale numeric
    URLs 404."""

    def setUp(self):
        self.owner = User.objects.create_user(username="o", password="p")
        self.tournament = Tournament.objects.create(
            name="Spring Open", location="x", start_date=date(2026, 1, 1), owner=self.owner
        )
        self.division = Division.objects.create(name="Open", tournament=self.tournament)

    def test_old_division_slug_redirects_after_rename(self):
        old_slug = self.division.slug
        self.division.name = "Masters"
        self.division.save()
        url = reverse("division_entrants", kwargs={
            "tournament_slug": self.tournament.slug, "division_slug": old_slug,
        })
        response = self.client.get(url)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            reverse("division_entrants", kwargs=self.division.slug_kwargs()),
        )

    def test_old_tournament_slug_redirects_after_rename(self):
        old_slug = self.tournament.slug
        self.tournament.name = "Summer Open"
        self.tournament.save()
        response = self.client.get(
            reverse("tournament_detail", kwargs={"tournament_slug": old_slug})
        )
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            reverse("tournament_detail", kwargs={"tournament_slug": self.tournament.slug}),
        )

    def test_query_string_preserved_on_redirect(self):
        old_slug = self.division.slug
        self.division.name = "Masters"
        self.division.save()
        url = reverse("resultslip_create", kwargs={
            "tournament_slug": self.tournament.slug, "division_slug": old_slug,
        }) + "?pairing=5"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].endswith("?pairing=5"))

    def test_stale_numeric_tournament_url_404s(self):
        response = self.client.get(f"/tournaments/{self.tournament.pk}/")
        self.assertEqual(response.status_code, 404)
