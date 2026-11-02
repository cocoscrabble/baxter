"""Round tabs grouped by day (DivisionSettings.games_per_day)."""

from django.test import TestCase
from django.urls import reverse

from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import Division
from tournaments.templatetags.round_days import day_picker
from tournaments.tests.test_byes import make_division
from users.models import User


class DayOfRoundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.division = make_division(self.user, 4, 3)

    def test_rounds_fall_into_days_of_games_per_day(self):
        days = [self.division.day_of_round(r) for r in (1, 7, 8, 14, 15)]
        self.assertEqual(days, [1, 1, 2, 2, 3])

    def test_a_division_without_settings_uses_the_default(self):
        other = Division.objects.create(tournament=self.division.tournament, name="E")
        self.assertEqual(other.day_of_round(8), 2)

    def test_divisions_of_one_tournament_can_differ(self):
        other = make_division(self.user, 4, 3, first_number=100)
        other.settings.games_per_day = 4
        other.settings.save()
        self.assertEqual(self.division.day_of_round(8), 2)
        self.assertEqual(other.day_of_round(8), 2)
        self.assertEqual(other.day_of_round(9), 3)

    def test_no_picker_for_a_one_day_schedule(self):
        self.assertEqual(day_picker(self.division, 7)["days"], [])

    def test_picker_starts_on_the_selected_rounds_day(self):
        ctx = day_picker(self.division, 20, 9)
        self.assertEqual(list(ctx["days"]), [1, 2, 3])
        self.assertEqual(ctx["default_day"], 2)


class DayTabsOnPagesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        # A round robin pairs and publishes all three rounds at once.
        self.division = make_division(self.user, 4, 3, pairing="RoundRobin")
        self.client.force_login(self.user)
        self.slugs = (self.division.tournament.slug, self.division.slug)
        self.client.post(
            reverse("division_settings", args=self.slugs),
            {"schedule": "1", "games_per_day": 2},
        )

    def test_the_settings_page_saves_games_per_day(self):
        self.division.settings.refresh_from_db()
        self.assertEqual(self.division.settings.games_per_day, 2)

    def test_pair_rounds_tabs_carry_their_day(self):
        body = self.client.get(
            reverse("division_pair_rounds", args=self.slugs)
        ).content.decode()
        self.assertIn('data-bind="roundDay"', body)
        self.assertIn('data-day="1"', body)
        self.assertIn('data-day="2"', body)

    def test_published_pairings_get_a_picker_once_they_span_two_days(self):
        regenerate_pairings(self.division)
        publish_rounds(self.division)
        body = self.client.get(
            reverse("published_pairings", args=self.slugs)
        ).content.decode()
        self.assertIn("Day 2", body)
