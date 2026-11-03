"""Tournament time zones: set on the tournament form, shown on its pages."""

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Tournament, TournamentEvent
from tournaments.replay import events_from_tournament, replay
from users.models import User


class TournamentTimeZoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="td", password="pw")
        self.client.force_login(self.user)

    def _create(self, **extra):
        self.client.post(reverse("tournament_create"), {
            "name": "Zoned", "location": "Toronto", "start_date": "2026-01-10",
            "editor_usernames": "", **extra,
        })
        return Tournament.objects.get(name="Zoned")

    def test_defaults_to_central_time(self):
        self.assertEqual(self._create().time_zone, "America/Chicago")

    def test_editing_sets_the_zone_and_replays(self):
        t = self._create(time_zone="America/Toronto")
        self.assertEqual(t.time_zone, "America/Toronto")
        self.client.post(reverse("tournament_edit", kwargs=t.slug_kwargs()), {
            "name": "Zoned", "location": "Toronto", "start_date": "2026-01-10",
            "time_zone": "Europe/London", "editor_usernames": "",
        })
        t.refresh_from_db()
        self.assertEqual(t.time_zone, "Europe/London")
        replayed = replay(events_from_tournament(t)).tournament
        self.assertEqual(replayed.time_zone, "Europe/London")

    def test_an_update_logged_without_a_zone_keeps_it(self):
        from tournaments.commands import update_tournament

        t = self._create(time_zone="America/Toronto")
        update_tournament(t, self.user, {
            "name": "Zoned", "location": "Toronto", "start_date": "2026-01-10",
        })
        t.refresh_from_db()
        self.assertEqual(t.time_zone, "America/Toronto")

    def test_the_activity_log_shows_times_in_the_tournament_zone(self):
        t = self._create(time_zone="America/Toronto")
        # 15:00 UTC in January is 10:00 EST.
        TournamentEvent.objects.filter(tournament=t).update(
            created_at=datetime(2026, 1, 10, 15, 0, tzinfo=dt_timezone.utc)
        )
        response = self.client.get(
            reverse("tournament_activity", kwargs=t.slug_kwargs())
        )
        self.assertContains(response, "10 Jan 2026, 10:00 EST")

    def test_a_tournament_without_a_usable_zone_shows_central(self):
        t = self._create()
        TournamentEvent.objects.filter(tournament=t).update(
            created_at=datetime(2026, 1, 10, 15, 0, tzinfo=dt_timezone.utc)
        )
        url = reverse("tournament_activity", kwargs=t.slug_kwargs())
        for bad in ("", "Not/AZone"):
            Tournament.objects.filter(pk=t.pk).update(time_zone=bad)
            # 15:00 UTC in January is 09:00 CST.
            self.assertContains(self.client.get(url), "10 Jan 2026, 09:00 CST")
