"""What-if Explore: the pure explore_pairing function (result truncation,
seedings, actual-result decoration, odd-field bye) and the view (bounds, auth)."""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    Tournament,
)
from tournaments.whatif import decorate, explore_pairing
from users.models import User


class ExploreBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.tournament = Tournament.objects.create(
            name="T", location="x", start_date=date(2026, 1, 1), owner=self.owner
        )
        self.division = Division.objects.create(name="Open", tournament=self.tournament)
        self.ent = {}
        for i, (name, rating) in enumerate(
            [("Alice", 1600), ("Bob", 1500), ("Cara", 1400), ("Dan", 1300)], start=1
        ):
            p = Player.objects.create(name=name, player_number=str(i), rating=rating)
            self.ent[name] = Entrant.objects.create(
                division=self.division, player=p, number=i
            )
        DivisionSettings.objects.create(division=self.division, pairing_seed=0)

    def _slip(self, round_num, winner, loser, ws, ls):
        return ResultSlip.objects.create(
            division=self.division, round=round_num,
            winner=self.ent[winner], winner_score=ws,
            loser=self.ent[loser], loser_score=ls, winner_started=True,
        )

    def _pairs(self, target, based_on, strategy="KotH", seed=0):
        return {
            frozenset({p.first.name, p.second.name})
            for p in explore_pairing(self.division, target, strategy, based_on, seed)
        }


class ExplorePairingTests(ExploreBase):
    def setUp(self):
        super().setUp()
        # Round 1 played as seeded; round 2 played as some other pairing.
        self._slip(1, "Alice", "Bob", 500, 300)
        self._slip(1, "Cara", "Dan", 450, 400)
        self._slip(2, "Alice", "Dan", 480, 420)
        self._slip(2, "Cara", "Bob", 460, 300)

    def test_round2_whatif_off_round1_ignores_round2_results(self):
        # Exploring round 2 off round 1 must not be influenced by round 2's own
        # slips (they are truncated). Changing a round-2 result leaves it identical.
        before = self._pairs(target=2, based_on=1)
        slip = ResultSlip.objects.get(round=2, winner=self.ent["Alice"])
        slip.winner_score, slip.loser_score = 301, 300  # flip the margin
        slip.save()
        after = self._pairs(target=2, based_on=1)
        self.assertEqual(before, after)
        # And it is not merely reproducing what actually happened in round 2.
        actual_round2 = {frozenset({"Alice", "Dan"}), frozenset({"Cara", "Bob"})}
        self.assertNotEqual(before, actual_round2)

    def test_based_on_zero_pairs_off_seedings(self):
        # KotH off seedings pairs by rating: Alice-Bob, Cara-Dan.
        self.assertEqual(
            self._pairs(target=1, based_on=0),
            {frozenset({"Alice", "Bob"}), frozenset({"Cara", "Dan"})},
        )

    def test_actual_result_is_decorated_when_pairing_really_happened(self):
        # Exploring round 1 off seedings reproduces the real round-1 pairing, so
        # each row carries the real score.
        pairings = explore_pairing(self.division, 1, "KotH", 0, 0)
        rows = decorate(self.division, 1, 0, pairings)
        ab = next(r for r in rows if {r.first, r.second} == {"Alice", "Bob"})
        self.assertIn("500", ab.result)
        self.assertIn("300", ab.result)
        # A pairing that did not happen carries no result.
        rows2 = decorate(self.division, 2, 1, explore_pairing(self.division, 2, "KotH", 1, 0))
        self.assertTrue(all(r.result == "" for r in rows2))


class ExploreOddFieldTests(ExploreBase):
    def test_odd_field_gets_a_bye_row(self):
        p = Player.objects.create(name="Eve", player_number="5", rating=1200)
        Entrant.objects.create(division=self.division, player=p, number=5)
        rows = decorate(
            self.division, 1, 0, explore_pairing(self.division, 1, "KotH", 0, 0)
        )
        bye_rows = [r for r in rows if r.second == "Bye"]
        self.assertEqual(len(bye_rows), 1)
        self.assertIsNone(bye_rows[0].table)


class ExploreViewTests(ExploreBase):
    def setUp(self):
        super().setUp()
        self._slip(1, "Alice", "Bob", 500, 300)
        self._slip(1, "Cara", "Dan", 450, 400)
        self.url = reverse("division_explore", kwargs=self.division.slug_kwargs())

    def test_editor_only(self):
        # Anonymous is redirected to login.
        self.assertEqual(self.client.get(self.url).status_code, 302)
        # A logged-in non-editor is forbidden.
        User.objects.create_user(username="stranger", password="pw")
        self.client.login(username="stranger", password="pw")
        self.assertIn(self.client.get(self.url).status_code, (302, 403))

    def test_defaults_and_bounds(self):
        self.client.force_login(self.owner)
        # max_round is 1, so target defaults to 1 and based_on to 0 (seedings).
        response = self.client.get(self.url)
        self.assertEqual(response.context["target_round"], 1)
        self.assertEqual(response.context["based_on"], 0)
        # Out-of-range params are clamped: round to max_round+1, based_on < target.
        response = self.client.get(self.url, {"round": 99, "based_on": 99})
        self.assertEqual(response.context["target_round"], 2)  # max_round(1) + 1
        self.assertEqual(response.context["based_on"], 1)

    def test_renders_whatif_table(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"round": 2, "strategy": "Swiss", "based_on": 1})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "What-if")
        self.assertContains(response, "Swiss off round 1 standings")
