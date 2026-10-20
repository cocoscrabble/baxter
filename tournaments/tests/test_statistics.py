"""Division statistics (tournaments/statistics.py), ported from the sheet's Stats
tab, and the Stats page that shows them."""

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Entrant, Player, ResultSlip
from tournaments.statistics import division_statistics

from .test_views import setUpTournament


class StatisticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        cls.player3 = Player.objects.create(name="Cara", player_number="003", rating=1400)
        cls.player4 = Player.objects.create(name="Dan", player_number="004", rating=1300)
        cls.entrant3 = Entrant.objects.create(division=cls.division, player=cls.player3, number=3)
        cls.entrant4 = Entrant.objects.create(division=cls.division, player=cls.player4, number=4)

    def _game(self, round, winner, loser, winner_score, loser_score):
        return ResultSlip.objects.create(
            division=self.division, round=round, winner=winner, loser=loser,
            winner_score=winner_score, loser_score=loser_score, winner_started=True,
        )

    def _stats(self, **kwargs):
        return {c.title: c.rows for c in division_statistics(self.division, **kwargs)}

    def _games(self, rows):
        return [(r.player.player.name, r.score, r.opponent_score) for r in rows]

    def test_each_category_ranks_from_its_own_side(self):
        a, b, c, d = self.entrant1, self.entrant2, self.entrant3, self.entrant4
        self._game(1, a, b, 500, 300)  # blowout, high win
        self._game(1, c, d, 410, 400)  # nailbiter, high loss
        self._game(2, b, d, 350, 280)  # low win, low loss
        stats = self._stats()

        self.assertEqual(
            self._games(stats["Highest wins"]),
            [("Alice", 500, 300), ("Cara", 410, 400), ("Bob", 350, 280)],
        )
        # A loss is told from the loser's side.
        self.assertEqual(self._games(stats["Highest losses"])[0], ("Dan", 400, 410))
        self.assertEqual(self._games(stats["Lowest losses"])[0], ("Dan", 280, 350))
        self.assertEqual(self._games(stats["Lowest wins"])[0], ("Bob", 350, 280))
        self.assertEqual(
            [r.value for r in stats["Highest games"]], [810, 800, 630]
        )
        self.assertEqual([r.value for r in stats["Biggest blowouts"]], [200, 70, 10])
        self.assertEqual([r.value for r in stats["Nailbiters"]], [10, 70, 200])
        self.assertEqual(stats["Nailbiters"][0].value_label, "Spread")

    def test_equal_values_share_a_rank_earlier_rounds_first(self):
        late = self._game(2, self.entrant1, self.entrant2, 450, 400)
        early = self._game(1, self.entrant3, self.entrant4, 450, 300)
        self._game(1, self.entrant1, self.entrant3, 420, 410)
        rows = self._stats()["Highest wins"]
        self.assertEqual([r.rank for r in rows], [1, 1, 3])
        self.assertEqual([r.round for r in rows[:2]], [early.round, late.round])

    def test_a_tie_is_not_a_win_or_a_loss(self):
        # The sheet listed ties under the win columns; they get their own list.
        self._game(1, self.entrant1, self.entrant2, 400, 400)
        stats = self._stats()
        self.assertEqual(set(stats), {"Highest games", "Ties"})
        self.assertEqual(self._games(stats["Ties"]), [("Alice", 400, 400)])

    def test_byes_and_forfeits_are_not_games(self):
        bye = self.division.bye_entrant()
        self._game(1, self.entrant1, bye, 50, 0)  # a bye
        self._game(1, bye, self.entrant2, 50, 0)  # a forfeit
        self.assertEqual(division_statistics(self.division), [])

    def test_only_the_top_ten(self):
        for i in range(12):
            self._game(i + 1, self.entrant1, self.entrant2, 400 + i, 300)
        rows = self._stats()["Highest wins"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0].score, 411)

    def test_stats_page_lists_the_categories(self):
        self._game(1, self.entrant1, self.entrant2, 500, 300)
        response = self.client.get(
            reverse("division_statistics", kwargs=self.division.slug_kwargs())
        )
        self.assertEqual(
            [c.title for c in response.context["categories"]],
            ["Highest wins", "Highest losses", "Lowest wins", "Lowest losses",
             "Highest games", "Biggest blowouts", "Nailbiters"],
        )
        self.assertContains(response, 'class="nav-tab active">Stats</a>')
