"""The embeddable standings: the CoCo site's results format, served bare.

The format was checked against the sample the CoCo site uses
(standings-embed.html, Charlottesville Autumn One-Day 2026) on a copy of
production: identical apart from three CSS fixes (see the template) and the
record and spread, which now count the byes. These tests pin what makes it an embed, and where its values come
from.
"""

from datetime import date

from django.test import TestCase, override_settings
from django.urls import reverse

from tournaments.live_ratings import project_ratings
from tournaments.models import Entrant, Player, ResultSlip
from tournaments.tests.test_views import setUpTournament


@override_settings(PLAYERDB_URL="https://playerdb.example.org")
class StandingsEmbedTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        for entrant, rating in ((self.entrant1, 1600), (self.entrant2, 1500)):
            entrant.rating = rating
            entrant.deviation = 80.0
            entrant.career_games = entrant.rated_games = 200
            entrant.last_played = date(2026, 1, 1)
            entrant.save()
        ResultSlip.objects.create(
            division=self.division, round=1,
            winner=self.entrant2, winner_score=450,
            loser=self.entrant1, loser_score=380, winner_started=True,
        )

    def _url(self):
        return reverse("division_standings_embed", kwargs=self.division.slug_kwargs())

    def _body(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_it_is_the_cocos_results_table(self):
        body = self._body()
        # The original's CSS bugs stay fixed.
        self.assertIn("table.coco-results {\n    border-collapse: collapse;", body)
        self.assertIn("white-space: nowrap;", body)
        self.assertNotIn('style="coco-results-nobreak"', body)
        self.assertTrue(body.startswith('<style type="text/css">'))
        self.assertIn('<table border="0" class="coco-results"', body)
        for heading in ("Player", "Record", "Spread", "Initial Rating", "New Rating"):
            self.assertIn(f"<td><b>{heading}&nbsp;</b></td>", body)
        self.assertNotIn("<nav", body)

    def test_a_row_carries_rank_link_record_spread_and_ratings(self):
        bob = project_ratings(self.division)[self.entrant2.key]
        self.assertIn(
            '\t<tr>\n'
            '\t\t<td align="left">1.</td>\n'
            '\t\t<td class="coco-results-nobreak" align="left">'
            '<a href="https://playerdb.example.org/player/2/bob/">Bob</a></td>\n'
            '\t\t<td align="left">1-0</td>\n'
            '\t\t<td align="left">70</td>\n'
            f'\t\t<td align="left">1500</td>\n'
            f'\t\t<td align="left">{bob.new_rating}</td>\n'
            '\t</tr>\n',
            self._body(),
        )
        self.assertGreater(bob.new_rating, 1500)

    def test_a_guest_has_no_player_page_to_link_to(self):
        guest = Player.objects.create(name="Gail", player_number="T-5", rating=0)
        Entrant.objects.create(division=self.division, player=guest, number=3)
        body = self._body()
        self.assertIn('align="left">Gail</td>', body)
        self.assertNotIn("/player/T-5/", body)

    def test_it_may_be_framed(self):
        response = self.client.get(self._url())
        self.assertNotIn("X-Frame-Options", response.headers)

    def test_a_test_division_is_hidden_even_from_its_own_editor(self):
        # The iframe carries the visitor's cookies; it must not vary by viewer.
        self.division.is_test = True
        self.division.save(update_fields=["is_test"])
        self.client.login(username="owner", password="testpass123")
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_editors_are_shown_the_link_on_the_standings_page(self):
        standings = reverse("division_standings", kwargs=self.division.slug_kwargs())
        self.assertNotContains(self.client.get(standings), self._url())
        self.client.login(username="owner", password="testpass123")
        self.assertContains(self.client.get(standings), self._url())
