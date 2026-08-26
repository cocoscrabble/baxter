"""The live rating projection (plans/PLAN_COCO_PROGRAM.md, "Live rating projection").

The projection's job is to predict what the official run will do to a player's
rating, so the tests that matter most are the ones comparing it against the real
engine on real tournaments — `test_matches_the_engine.py` does that across the
whole results corpus. These cover the assembly: what gets seeded, what gets
skipped, and what happens at the edges.
"""

from datetime import date

from django.test import TestCase

from tournaments.live_ratings import project_ratings
from tournaments.models import (
    Division,
    Entrant,
    Pairing,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from users.models import User


class ProjectionTestCase(TestCase):
    START = date(2026, 6, 1)

    def setUp(self):
        self.owner = User.objects.create_user(username="td-proj", password="pw")
        self.tournament = Tournament.objects.create(
            name="Champs", location="X", start_date=self.START, owner=self.owner
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        self._seat = 0

    def player(self, name, rating=1600, deviation=80.0, games=200,
               last_played=date(2026, 5, 1), number=None):
        self._seat += 1
        return Player.objects.create(
            name=name,
            player_number=number or f"{self._seat:04d}",
            rating=rating,
            deviation=deviation,
            career_games=games,
            last_played=last_played,
        )

    def enter(self, player, **kwargs):
        return Entrant.enter(self.division, player, self._seat, **kwargs)

    def game(self, round, winner, loser, winner_score=450, loser_score=380):
        rp, _ = RoundPairings.objects.get_or_create(
            division=self.division, round=round,
            defaults={"status": RoundPairings.FINISHED},
        )
        pairing = Pairing.objects.create(
            division=self.division, round=round, round_pairings=rp,
            first=winner, second=loser, table=1,
        )
        return ResultSlip.objects.create(
            division=self.division, round=round, pairing=pairing,
            winner=winner, winner_score=winner_score,
            loser=loser, loser_score=loser_score, winner_started=True,
        )


class BasicProjectionTests(ProjectionTestCase):
    def test_a_division_with_no_entrants_projects_nothing(self):
        self.assertEqual(project_ratings(self.division), {})

    def test_a_division_with_no_games_projects_nothing(self):
        """Returning everyone's unchanged rating would look like a result."""
        self.enter(self.player("Ann"))
        self.enter(self.player("Bea"))
        self.assertEqual(project_ratings(self.division), {})

    def test_the_winner_gains_and_the_loser_loses(self):
        ann = self.enter(self.player("Ann", rating=1600))
        bea = self.enter(self.player("Bea", rating=1600))
        self.game(1, ann, bea)

        projections = project_ratings(self.division)
        self.assertEqual(set(projections), {ann.key, bea.key})
        self.assertGreater(projections[ann.key].delta, 0)
        self.assertLess(projections[bea.key].delta, 0)
        # Evenly matched, so the moves mirror each other.
        self.assertEqual(
            projections[ann.key].delta, -projections[bea.key].delta
        )

    def test_the_record_and_spread_come_back(self):
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea, 500, 400)
        self.game(2, bea, ann, 450, 430)

        projections = project_ratings(self.division)
        ann_p = projections[ann.key]
        self.assertEqual((ann_p.wins, ann_p.losses), (1.0, 1.0))
        self.assertEqual(ann_p.spread, 100 - 20)
        self.assertEqual(ann_p.games, 2)

    def test_a_tie_counts_half_to_each(self):
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea, 420, 420)
        projections = project_ratings(self.division)
        self.assertEqual(projections[ann.key].wins, 0.5)
        self.assertEqual(projections[ann.key].losses, 0.5)

    def test_it_is_keyed_on_the_player_number(self):
        ann = self.enter(self.player("Ann", number="0233"))
        bea = self.enter(self.player("Bea", number="0517"))
        self.game(1, ann, bea)
        self.assertEqual(set(project_ratings(self.division)), {"0233", "0517"})

    def test_two_entrants_sharing_a_name_are_projected_separately(self):
        a = self.enter(self.player("John Smith", rating=1800, number="0010"))
        b = self.enter(self.player("John Smith", rating=1400, number="0011"))
        self.game(1, a, b)
        projections = project_ratings(self.division)
        self.assertEqual(len(projections), 2)
        self.assertNotEqual(
            projections["0010"].new_rating, projections["0011"].new_rating
        )

    def test_nothing_is_written(self):
        """Derived, never stored."""
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea)
        before = {
            e.pk: (e.rating, e.deviation, e.career_games)
            for e in self.division.entrants.all()
        }
        project_ratings(self.division)
        after = {
            e.pk: (e.rating, e.deviation, e.career_games)
            for e in Entrant.objects.filter(division=self.division)
        }
        self.assertEqual(after, before)
        self.assertEqual(
            Player.objects.get(name="Ann").rating, 1600, "player untouched"
        )


class SeedTests(ProjectionTestCase):
    def test_it_projects_from_the_frozen_snapshot_not_the_player(self):
        """The reason the snapshot exists: a roster pull mid-tournament must not
        move anyone's projection."""
        ann_player = self.player("Ann", rating=1600)
        ann = self.enter(ann_player)
        bea = self.enter(self.player("Bea", rating=1600))
        self.game(1, ann, bea)
        before = project_ratings(self.division)[ann.key]

        ann_player.rating = 1900
        ann_player.deviation = 40.0
        ann_player.career_games = 900
        ann_player.save()

        self.assertEqual(project_ratings(self.division)[ann.key], before)

    def test_deviation_is_aged_to_the_tournament_date(self):
        """Deviation grows with inactivity; skipping that makes the projection
        systematically wrong for returning players."""
        recent = self.enter(
            self.player("Recent", deviation=60.0, last_played=date(2026, 5, 25))
        )
        absent = self.enter(
            self.player("Absent", deviation=60.0, last_played=date(2020, 1, 1))
        )
        self.game(1, recent, absent)

        projections = project_ratings(self.division)
        # The long-absent player is rated with a much wider deviation, so the
        # same result moves them further.
        self.assertGreater(
            abs(projections[absent.key].delta), abs(projections[recent.key].delta)
        )

    def test_an_entrant_with_no_recorded_deviation_gets_the_maximum(self):
        """A snapshot taken before the roster pull existed carries none."""
        ann = self.enter(self.player("Ann", deviation=None))
        bea = self.enter(self.player("Bea", deviation=None))
        self.game(1, ann, bea)
        projections = project_ratings(self.division)
        self.assertTrue(projections[ann.key].new_deviation > 0)

    def test_as_of_overrides_the_tournament_date(self):
        ann = self.enter(self.player("Ann", last_played=date(2026, 5, 1)))
        bea = self.enter(self.player("Bea", last_played=date(2026, 5, 1)))
        self.game(1, ann, bea)
        near = project_ratings(self.division, as_of=date(2026, 6, 1))
        far = project_ratings(self.division, as_of=date(2030, 6, 1))
        self.assertNotEqual(near[ann.key].new_rating, far[ann.key].new_rating)


class UnratedTests(ProjectionTestCase):
    def test_an_unrated_entrant_has_a_rating_established(self):
        """Through calc_initial_ratings — the same convergence loop the official
        run uses, not a single pass."""
        rated = self.enter(self.player("Rated", rating=1800))
        unrated = self.enter(
            self.player("Unrated", rating=0, deviation=None, games=0,
                        last_played=None)
        )
        for round in (1, 2, 3):
            self.game(round, rated, unrated)

        projections = project_ratings(self.division)
        self.assertTrue(projections[unrated.key].was_unrated)
        self.assertGreater(projections[unrated.key].new_rating, 0)
        self.assertFalse(projections[rated.key].was_unrated)

    def test_a_newly_rated_entrant_has_no_delta(self):
        """Not a quirk — it is what the official run records.

        ``ratings.TournamentResult`` stores old == new for a player's first
        tournament, and this projection exists to predict what that run will
        say. ``was_unrated`` is the flag that explains the zero.
        """
        rated = self.enter(self.player("Rated", rating=1600))
        unrated = self.enter(self.player("Unrated", rating=0))
        self.game(1, rated, unrated)

        projection = project_ratings(self.division)[unrated.key]
        self.assertTrue(projection.was_unrated)
        self.assertEqual(projection.old_rating, projection.new_rating)
        self.assertEqual(projection.delta, 0)
        # …and it is a real rating, not the 1500 seed left in place.
        self.assertNotEqual(projection.new_rating, 1500)


class ByeTests(ProjectionTestCase):
    def test_a_bye_is_not_a_game(self):
        """Not merely ignored by the math — excluded, so it cannot count toward
        career games, which feed the rating multiplier."""
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea)

        bye = self.division.bye_entrant()
        self.game(2, ann, bye, 50, 0)

        projections = project_ratings(self.division)
        self.assertEqual(projections[ann.key].games, 1)
        self.assertNotIn("BYE", projections)

    def test_a_division_of_only_byes_projects_nothing(self):
        ann = self.enter(self.player("Ann"))
        self.game(1, ann, self.division.bye_entrant(), 50, 0)
        self.assertEqual(project_ratings(self.division), {})


class WithdrawnTests(ProjectionTestCase):
    def test_a_dropped_entrant_is_still_projected(self):
        """Their games were played and still count — for them and everyone."""
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea)
        bea.dropped = True
        bea.save(update_fields=["dropped"])

        projections = project_ratings(self.division)
        self.assertIn(bea.key, projections)
        self.assertEqual(projections[bea.key].games, 1)


class LiveRatingsViewTests(ProjectionTestCase):
    def _url(self):
        from django.urls import reverse

        return reverse(
            "division_live_ratings", kwargs=self.division.slug_kwargs()
        )

    def test_an_empty_division_says_there_is_nothing_yet(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No completed games yet")

    def test_it_lists_projections_best_first(self):
        top = self.enter(self.player("Top", rating=1900))
        mid = self.enter(self.player("Mid", rating=1600))
        low = self.enter(self.player("Low", rating=1300))
        self.game(1, top, mid)
        self.game(2, mid, low)

        response = self.client.get(self._url())
        names = [r["projection"].name for r in response.context["rows"]]
        self.assertEqual(names, ["Top", "Mid", "Low"])

    def test_it_says_the_numbers_are_provisional(self):
        """The one thing this page must never let a reader forget."""
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea)
        response = self.client.get(self._url())
        self.assertContains(response, "Provisional")

    def test_a_newly_rated_player_shows_no_before_and_no_change(self):
        rated = self.enter(self.player("Rated", rating=1600))
        self.enter(self.player("Unrated", rating=0))
        self.game(1, rated, self.division.entrants.get(player__name="Unrated"))

        response = self.client.get(self._url())
        self.assertTrue(response.context["has_unrated"])
        self.assertContains(response, "Rating established by this tournament")
        self.assertContains(response, "new")

    def test_it_is_visible_without_signing_in(self):
        ann = self.enter(self.player("Ann"))
        bea = self.enter(self.player("Bea"))
        self.game(1, ann, bea)
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_a_test_division_is_still_hidden(self):
        self.division.is_test = True
        self.division.save(update_fields=["is_test"])
        self.assertEqual(self.client.get(self._url()).status_code, 404)


class NoRateableGamesTests(ProjectionTestCase):
    """A player whose every game is skipped by the calculator.

    All byes, or all forfeits. The engine skips a game with a zero score, so
    such a player is never actually rated — and must not come back with a
    projected rating of 0.
    """

    def test_an_unrated_player_with_only_forfeits_reports_the_seed(self):
        rated = self.enter(self.player("Rated", rating=1600))
        forfeiter = self.enter(self.player("Forfeit", rating=0))
        # A forfeit: the loser scores nothing, so the calculator skips it.
        self.game(1, rated, forfeiter, 100, 0)

        projection = project_ratings(self.division)[forfeiter.key]
        self.assertTrue(projection.was_unrated)
        self.assertEqual(
            projection.new_rating, 1500,
            "an unrated player who was never rated sits at the seed, not 0",
        )

    def test_a_rated_player_with_only_forfeits_is_unchanged(self):
        rated = self.enter(self.player("Rated", rating=1600))
        unlucky = self.enter(self.player("Unlucky", rating=1450))
        self.game(1, rated, unlucky, 100, 0)

        projection = project_ratings(self.division)[unlucky.key]
        self.assertEqual(projection.delta, 0)
        self.assertEqual(projection.new_rating, projection.old_rating)
