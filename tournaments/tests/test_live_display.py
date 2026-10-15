"""The live display pages: chrome-free, self-refreshing standings, pairings and
recent results for a screen at the venue."""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from tournaments.models import Entrant, Pairing, Player, ResultSlip, RoundPairings

from tournaments.views import live_columns, split_columns

from .test_views import setUpTournament


class LiveLayoutTests(SimpleTestCase):
    def test_small_field_stays_in_one_column(self):
        self.assertEqual(live_columns(8, 26), 1)

    def test_larger_field_spreads_across_columns(self):
        self.assertGreater(live_columns(80, 26), 1)
        self.assertLessEqual(live_columns(500, 26), 4)

    def test_wider_tables_get_fewer_columns(self):
        self.assertLessEqual(live_columns(80, 38), live_columns(80, 26))

    def test_split_reads_down_then_across(self):
        self.assertEqual(split_columns(list(range(7)), 3), [[0, 1, 2], [3, 4, 5], [6]])
        self.assertEqual(split_columns([], 2), [])


class LiveDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.player3 = Player.objects.create(name="Cara", player_number="003", rating=1400)
        cls.player4 = Player.objects.create(name="Dan", player_number="004", rating=1300)
        cls.entrant3 = Entrant.objects.create(division=cls.division, player=cls.player3, number=3)
        cls.entrant4 = Entrant.objects.create(division=cls.division, player=cls.player4, number=4)

    def _url(self, name):
        return reverse(name, kwargs=self.division.slug_kwargs())

    def _create_round(self, round_num, status, pairs):
        rp = RoundPairings.objects.create(division=self.division, round=round_num, status=status)
        for table, (first, second) in enumerate(pairs, start=1):
            Pairing.objects.create(
                division=self.division, round=round_num, round_pairings=rp,
                first=first, second=second, table=table,
            )

    def _slip(self, round_num, winner, loser, created_at):
        slip = ResultSlip.objects.create(
            division=self.division, round=round_num, winner=winner, loser=loser,
            winner_score=400, loser_score=300, winner_started=True,
        )
        # auto_now_add ignores a passed value, so set it afterwards.
        ResultSlip.objects.filter(pk=slip.pk).update(created_at=created_at)
        return slip

    def test_pages_have_no_site_nav(self):
        for name in ("live_standings", "live_pairings", "live_results"):
            response = self.client.get(self._url(name))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'class="navbar"')
            self.assertContains(response, "data-on-interval__duration.15s")

    def test_poll_returns_only_the_content_fragment(self):
        response = self.client.get(
            self._url("live_standings"), headers={"Datastar-Request": "true"}
        )
        body = b"".join(response.streaming_content).decode()
        self.assertIn('id="live-content"', body)
        self.assertNotIn("data-on-interval", body)

    def test_pairings_show_latest_non_draft_round_including_finished(self):
        # Between rounds the display keeps showing the round just played, and a
        # draft (not yet printed) round is never shown.
        self._create_round(1, RoundPairings.FINISHED, [(self.entrant1, self.entrant2)])
        self._create_round(2, RoundPairings.DRAFT, [(self.entrant1, self.entrant3)])
        response = self.client.get(self._url("live_pairings"))
        self.assertEqual(response.context["round"], 1)

        RoundPairings.objects.filter(division=self.division, round=2).update(
            status=RoundPairings.PUBLISHED
        )
        response = self.client.get(self._url("live_pairings"))
        self.assertEqual(response.context["round"], 2)
        firsts = [e.pairing.first for e in response.context["live_rows"]]
        self.assertEqual(firsts, [self.entrant1])

    def test_pairings_board_offers_no_submit_link(self):
        # The live board shares the public pairings table, which links unplayed
        # games to the result form; the screen must not.
        self._create_round(1, RoundPairings.PUBLISHED, [(self.entrant1, self.entrant2)])
        submit = reverse("resultslip_create", kwargs=self.division.slug_kwargs())
        self.assertContains(self.client.get(self._url("published_pairings")), submit)
        self.assertNotContains(self.client.get(self._url("live_pairings")), submit)

    def test_results_newest_first_without_byes(self):
        now = timezone.now()
        self._create_round(1, RoundPairings.FINISHED, [])
        older = self._slip(1, self.entrant1, self.entrant2, now - timedelta(minutes=5))
        newer = self._slip(1, self.entrant3, self.entrant4, now)
        bye = self.division.bye_entrant()
        self._slip(1, self.entrant1, bye, now + timedelta(minutes=1))
        response = self.client.get(self._url("live_results"))
        self.assertEqual(list(response.context["live_rows"]), [newer, older])

    def test_standings_ranks_run_on_across_columns(self):
        response = self.client.get(self._url("live_standings"))
        ranks = [p.rank for column in response.context["live_columns"] for p in column]
        self.assertEqual(ranks, list(range(1, len(ranks) + 1)))
        self.assertContains(response, "--live-rows: 4;")

    def test_editor_sees_no_editor_columns(self):
        self.client.login(username="owner", password="testpass123")
        response = self.client.get(self._url("live_standings"))
        self.assertFalse(response.context["can_edit"])

    def test_live_menu_in_public_division_nav(self):
        response = self.client.get(self._url("division_standings"))
        for name in ("live_standings", "live_pairings", "live_results"):
            self.assertContains(response, self._url(name))
