"""Display disambiguation (plans/PLAN_PLAYER_IDENTITY.md phase 5).

A name renders bare; the player number is appended only when someone else in
the same scope shares it. The scope is the point of most of these tests: a
clash the reader cannot see is not one they need warning about, but a clash in
a picker that offers both people is.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.display import display_names, division_labels
from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    Tournament,
)
from users.models import User


class DisplayNamesTests(TestCase):
    def _players(self, *pairs):
        return [
            Player(name=name, player_number=number) for name, number in pairs
        ]

    def test_a_unique_name_renders_bare(self):
        players = self._players(("Ann", "0001"), ("Bea", "0002"))
        self.assertEqual(
            display_names(players), {"0001": "Ann", "0002": "Bea"}
        )

    def test_a_shared_name_carries_the_number_on_both(self):
        players = self._players(
            ("John Smith", "0001"), ("John Smith", "0002"), ("Bea", "0003")
        )
        self.assertEqual(
            display_names(players),
            {
                "0001": "John Smith (0001)",
                "0002": "John Smith (0002)",
                "0003": "Bea",
            },
        )

    def test_the_clash_is_case_insensitive_but_each_keeps_its_spelling(self):
        players = self._players(("Ann", "0001"), ("ANN", "0002"))
        self.assertEqual(
            display_names(players),
            {"0001": "Ann (0001)", "0002": "ANN (0002)"},
        )

    def test_empty(self):
        self.assertEqual(display_names([]), {})


class DivisionScopeTests(TestCase):
    """Scope is the division for what a reader is looking at."""

    def setUp(self):
        self.owner = User.objects.create_user(username="td", password="pw")
        self.tournament = Tournament.objects.create(
            name="Champs", location="Reno",
            start_date=date(2026, 3, 15), owner=self.owner,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        DivisionSettings.objects.create(division=self.division)
        self.ann = Player.objects.create(
            name="Ann Lee", player_number="0001", rating=1600
        )
        self.bea = Player.objects.create(
            name="Bea Fox", player_number="0002", rating=1500
        )
        self.e_ann = Entrant.objects.create(
            division=self.division, player=self.ann, number=1
        )
        self.e_bea = Entrant.objects.create(
            division=self.division, player=self.bea, number=2
        )
        self.client.force_login(self.owner)

    def _url(self, name):
        return reverse(name, kwargs=self.division.slug_kwargs())

    def _add_twin(self, number="0003"):
        twin = Player.objects.create(
            name="Ann Lee", player_number=number, rating=1400
        )
        return twin

    def test_no_clash_renders_bare_names(self):
        labels = division_labels(self.division)
        self.assertEqual(
            set(labels.values()), {"Ann Lee", "Bea Fox"}
        )

    def test_a_clash_outside_the_division_changes_nothing(self):
        # A second Ann Lee exists, but not in this division — the reader of this
        # roster has no ambiguity to resolve.
        self._add_twin()
        labels = division_labels(self.division)
        self.assertEqual(labels[self.ann.pk], "Ann Lee")

    def test_a_clash_inside_the_division_marks_both_and_nobody_else(self):
        twin = self._add_twin()
        Entrant.objects.create(division=self.division, player=twin, number=3)
        labels = division_labels(self.division)
        self.assertEqual(labels[self.ann.pk], "Ann Lee (0001)")
        self.assertEqual(labels[twin.pk], "Ann Lee (0003)")
        self.assertEqual(labels[self.bea.pk], "Bea Fox")

    def test_the_entrants_page_disambiguates(self):
        twin = self._add_twin()
        Entrant.objects.create(division=self.division, player=twin, number=3)
        response = self.client.get(self._url("division_entrants"))
        self.assertContains(response, "Ann Lee (0001)")
        self.assertContains(response, "Ann Lee (0003)")
        self.assertContains(response, "Bea Fox")

    def test_the_entrants_page_is_unchanged_without_a_clash(self):
        response = self.client.get(self._url("division_entrants"))
        self.assertContains(response, "Ann Lee")
        self.assertNotContains(response, "(0001)")

    def test_results_disambiguate(self):
        twin = self._add_twin()
        e_twin = Entrant.objects.create(
            division=self.division, player=twin, number=3
        )
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.e_ann,
            winner_score=450, loser=e_twin, loser_score=380, winner_started=True,
        )
        response = self.client.get(self._url("division_all_results"))
        self.assertContains(response, "Ann Lee (0001)")
        self.assertContains(response, "Ann Lee (0003)")

    def test_standings_disambiguate(self):
        twin = self._add_twin()
        e_twin = Entrant.objects.create(
            division=self.division, player=twin, number=3
        )
        ResultSlip.objects.create(
            division=self.division, round=1, winner=self.e_ann,
            winner_score=450, loser=e_twin, loser_score=380, winner_started=True,
        )
        response = self.client.get(self._url("division_standings"))
        self.assertContains(response, "Ann Lee (0001)")
        self.assertContains(response, "Ann Lee (0003)")


class RosterScopeTests(TestCase):
    """Scope is the whole roster where the picker offers the whole roster."""

    def setUp(self):
        self.owner = User.objects.create_user(username="td", password="pw")
        self.tournament = Tournament.objects.create(
            name="Champs", location="Reno",
            start_date=date(2026, 3, 15), owner=self.owner,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        self.client.force_login(self.owner)

    def test_the_player_picker_judges_ambiguity_against_every_player(self):
        """Two Ann Lees who have never met in a division still need telling apart
        here — this picker is how one of them gets added in the first place."""
        from tournaments.grids import EntrantsGrid

        Player.objects.create(name="Ann Lee", player_number="0001", rating=1600)
        Player.objects.create(name="Ann Lee", player_number="0002", rating=1500)
        Player.objects.create(name="Bea Fox", player_number="0003", rating=1400)

        labels = {
            p["label"] for p in EntrantsGrid().lookups(self.division)["players"]
        }
        self.assertIn("Ann Lee (0001)", labels)
        self.assertIn("Ann Lee (0002)", labels)
        self.assertIn("Bea Fox", labels)
