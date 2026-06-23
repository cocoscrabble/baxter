from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.fake_tournament import create_fake_tournament
from tournaments.models import Player, RoundPairings, Tournament
from users.models import User


class CreateFakeTournamentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="u", password="p")
        for i in range(10):
            Player.objects.create(
                name=f"Player {i}", player_number=str(i), rating=1500 + i
            )

    def test_provisional_players_are_excluded_from_entrants(self):
        provisional_ids = set()
        for i in range(4):
            p = Player.objects.create(
                name=f"Prov {i}", player_number=f"T-{i}", rating=1500,
                is_provisional=True,
            )
            provisional_ids.add(p.pk)

        division = create_fake_tournament(self.user, num_players=6, num_rounds=2)

        entrant_player_ids = set(
            division.entrants.values_list("player_id", flat=True)
        )
        self.assertEqual(entrant_player_ids & provisional_ids, set())

    def test_fills_entrants_and_sets_all_rounds_koth(self):
        division = create_fake_tournament(self.user, num_players=6, num_rounds=4)

        # Not a test division: a fake tournament is visible to logged-out users.
        self.assertFalse(division.is_test)
        self.assertEqual(division.entrants.count(), 6)
        rps = division.settings.round_pairings
        self.assertEqual([rp["pairing"] for rp in rps], ["KotH"] * 4)

    def test_simulates_all_but_the_last_round(self):
        division = create_fake_tournament(self.user, num_players=6, num_rounds=4)

        # Rounds 1-3 are finished with a full set of results (6 players -> 3 games).
        finished = division.round_pairings_set.filter(status=RoundPairings.FINISHED)
        self.assertEqual(finished.count(), 3)
        for r in range(1, 4):
            self.assertEqual(division.result_slips.filter(round=r).count(), 3)

        # The final round is left pairable: no results and no pairings generated yet.
        self.assertEqual(division.result_slips.filter(round=4).count(), 0)
        self.assertFalse(division.round_pairings_set.filter(round=4).exists())

    def test_single_round_leaves_everything_pairable(self):
        division = create_fake_tournament(self.user, num_players=4, num_rounds=1)

        self.assertEqual(division.result_slips.count(), 0)
        self.assertFalse(division.round_pairings_set.exists())

    def test_large_field_simulates_every_round_fully(self):
        # 30 players over many rounds is where the Swiss implementation used to
        # return a partial round and stall the loop; King of the Hill always
        # pairs the whole field, so every simulated round must be full.
        for i in range(30):
            Player.objects.create(
                name=f"Extra {i}", player_number=f"E{i}", rating=1500 + i
            )
        num_players, num_rounds = 30, 20
        division = create_fake_tournament(self.user, num_players, num_rounds)

        n_games = num_players // 2
        for r in range(1, num_rounds):
            self.assertEqual(
                division.result_slips.filter(round=r).count(),
                n_games,
                f"round {r} was not fully simulated",
            )
        self.assertEqual(
            division.round_pairings_set.filter(status=RoundPairings.FINISHED).count(),
            num_rounds - 1,
        )
        self.assertFalse(division.round_pairings_set.filter(round=num_rounds).exists())


class FakeTournamentDeleteTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner_ft", password="p")
        cls.admin = User.objects.create_user(
            username="admin_ft", password="p", role=User.Role.ADMIN
        )
        cls.other = User.objects.create_user(username="other_ft", password="p")
        for i in range(4):
            Player.objects.create(
                name=f"FT {i}", player_number=f"FT{i}", rating=1500 + i
            )
        cls.fake = create_fake_tournament(cls.owner, num_players=4, num_rounds=2).tournament
        cls.real = Tournament.objects.create(
            name="Real", location="X", start_date=date.today(), owner=cls.owner
        )

    def test_can_delete_permissions(self):
        # Owner can delete either; admin can delete only the fake one; an
        # unrelated user can delete neither.
        self.assertTrue(self.fake.can_delete(self.owner))
        self.assertTrue(self.fake.can_delete(self.admin))
        self.assertFalse(self.fake.can_delete(self.other))

        self.assertTrue(self.real.can_delete(self.owner))
        self.assertFalse(self.real.can_delete(self.admin))
        self.assertFalse(self.real.can_delete(self.other))

    def test_admin_can_delete_fake_tournament_via_view(self):
        self.client.login(username="admin_ft", password="p")
        response = self.client.post(
            reverse("tournament_delete", kwargs={"pk": self.fake.pk})
        )
        self.assertRedirects(response, reverse("tournament_list"))
        self.assertFalse(Tournament.objects.filter(pk=self.fake.pk).exists())

    def test_admin_cannot_delete_real_tournament_via_view(self):
        self.client.login(username="admin_ft", password="p")
        response = self.client.post(
            reverse("tournament_delete", kwargs={"pk": self.real.pk})
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Tournament.objects.filter(pk=self.real.pk).exists())

    def test_delete_link_shown_only_for_fake_tournaments(self):
        self.client.login(username="admin_ft", password="p")
        html = self.client.get(reverse("tournament_list")).content.decode()
        self.assertIn(
            reverse("tournament_delete", kwargs={"pk": self.fake.pk}), html
        )
        self.assertNotIn(
            reverse("tournament_delete", kwargs={"pk": self.real.pk}), html
        )
