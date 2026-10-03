"""Clearing completed-round results must reopen the published board."""

import json

from django.test import TestCase
from django.urls import reverse

from tournaments.forfeits import forfeit_game
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import ResultSlip, RoundPairings
from tournaments.pairings_view import PairingsPresenter, PublishedPairingsPresenter
from tournaments.tests.test_byes import make_division
from users.models import User


class CompletedRoundResetTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="director", password="pw")
        self.division = make_division(self.owner, 6, 2)
        self.division.entrants.update(rating=1500, rating_source="manual")
        self.client.force_login(self.owner)
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        self.rp = self.division.round_pairings_set.get(round=1)

    def save_results(self, rows, version=0):
        response = self.client.post(
            reverse("division_edit_results", kwargs=self.division.slug_kwargs()),
            json.dumps({"rows": rows, "_version": version}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.rp.refresh_from_db()

    def test_clearing_every_forfeit_restores_unpublish_and_republish(self):
        # Jen's sequence: three published games, forfeit each one, then clear
        # the six absence results from the results grid.
        for pairing in list(self.division.pairings.filter(round=1)):
            forfeit_game(self.division.tournament, self.owner, {
                "division": self.division.name, "round": 1,
                "player": pairing.first.key,
            })
        self.rp.refresh_from_db()
        self.assertEqual(self.rp.status, RoundPairings.FINISHED)
        self.assertEqual(self.division.result_slips.count(), 6)
        self.save_results([])
        self.assertEqual(self.rp.status, RoundPairings.PUBLISHED)
        self.assertEqual(self.division.result_slips.count(), 0)
        self.assertIn("pairings", PublishedPairingsPresenter(self.division).as_context())
        self.assertTrue(PairingsPresenter(self.division).select(1).as_context()["can_unpublish_selected"])

        for route in ("unpublish_round", "publish_round"):
            response = self.client.post(
                reverse(route, kwargs=self.division.slug_kwargs()),
                {"round": 1}, follow=True,
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual(self.rp.status, RoundPairings.PUBLISHED)
        self.assertEqual(self.division.pairings.filter(round=1).count(), 3)
        self.assertFalse(self.division.pairings.filter(round=1, forfeit=True).exists())
        self.assertEqual(self.division.result_slips.count(), 0)

    def test_partial_deletion_reopens_finished_round_with_played_results(self):
        slips = []
        for pairing in self.division.pairings.filter(round=1):
            slips.append(ResultSlip.objects.create(
                division=self.division, round=1, pairing=pairing,
                winner=pairing.first, loser=pairing.second,
                winner_score=400, loser_score=350, winner_started=True,
            ))
        self.rp.update_status()
        self.assertEqual(self.rp.status, RoundPairings.FINISHED)
        kept = slips[0]
        self.save_results([{
            "round": 1, "winner": kept.winner_id, "loser": kept.loser_id,
            "winner_score": 400, "loser_score": 350, "winner_started": True,
        }])
        self.assertEqual(self.rp.status, RoundPairings.IN_PROGRESS)
        self.assertFalse(PairingsPresenter(self.division).select(1).as_context()["can_unpublish_selected"])
        self.assertIn("pairings", PublishedPairingsPresenter(self.division).as_context())
        self.save_results([], version=1)
        self.assertEqual(self.rp.status, RoundPairings.PUBLISHED)

    def test_status_refresh_does_not_publish_a_draft(self):
        draft = self.division.round_pairings_set.filter(status=RoundPairings.DRAFT).first()
        if draft is None:
            draft = RoundPairings.objects.create(division=self.division, round=2)
        draft.update_status()
        draft.refresh_from_db()
        self.assertEqual(draft.status, RoundPairings.DRAFT)
