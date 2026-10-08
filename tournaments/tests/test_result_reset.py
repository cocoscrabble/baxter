"""Clearing completed-round results must reopen the published board."""

import json

from django.test import TestCase
from django.urls import reverse

from tournaments.forfeits import forfeit_game
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import ResultSlip, RoundPairings
from tournaments.pairings_view import PairingsPresenter, PublishedPairingsPresenter
from tournaments.tests.test_byes import make_division
from tournaments.tests.test_replay import LoggedTournamentMixin
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

    def test_reset_discards_played_results_and_pairs_new_entrants(self):
        from tournaments.models import Entrant, Player

        pairing = self.division.pairings.filter(round=1).first()
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairing,
            winner=pairing.first, loser=pairing.second,
            winner_score=400, loser_score=350, winner_started=True,
        )
        self.rp.update_status()
        url = reverse("unpublish_round", kwargs=self.division.slug_kwargs())
        self.client.post(url, {"round": 1})
        self.assertEqual(self.division.result_slips.count(), 1)
        for n in (7, 8):
            p = Player.objects.create(name=f"New {n}", player_number=str(n), rating=1500)
            Entrant.objects.create(division=self.division, player=p, number=n,
                                   rating=1500, rating_source="manual")
        response = self.client.post(url, {"round": 1, "reset": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual(self.rp.status, RoundPairings.DRAFT)
        self.assertEqual(self.division.result_slips.count(), 0)
        self.assertEqual(self.division.pairings.filter(round=1).count(), 4)
        event = self.division.tournament.events.get(event_type="round_reset")
        self.assertEqual(event.payload, {"division": self.division.name, "round": 1})

    def test_reset_refuses_to_invalidate_later_published_rounds(self):
        from tournaments.commands import reset_round
        from tournaments.events import division_digest

        for pairing in self.division.pairings.filter(round=1):
            ResultSlip.objects.create(
                division=self.division, round=1, pairing=pairing,
                winner=pairing.first, loser=pairing.second,
                winner_score=400, loser_score=350, winner_started=True,
            )
        self.rp.update_status()
        regenerate_pairings(self.division)
        publish_rounds(self.division, [2])
        before = division_digest(self.division)
        with self.assertRaisesMessage(ValueError, "later rounds first"):
            reset_round(self.division.tournament, self.owner,
                        {"division": self.division.name, "round": 1})
        self.assertEqual(division_digest(self.division), before)

    def test_status_refresh_does_not_publish_a_draft(self):
        draft = self.division.round_pairings_set.filter(status=RoundPairings.DRAFT).first()
        if draft is None:
            draft = RoundPairings.objects.create(division=self.division, round=2)
        draft.update_status()
        draft.refresh_from_db()
        self.assertEqual(draft.status, RoundPairings.DRAFT)


class CompletedRoundResetReplayTests(LoggedTournamentMixin, TestCase):
    def test_forfeit_clear_and_republish_replays_with_matching_digests(self):
        from tournaments.events import division_digest
        from tournaments.replay import events_from_tournament, replay

        tournament, division = self._build_logged_tournament()
        for pairing in list(division.pairings.filter(round=1)):
            forfeit_game(tournament, self.owner, {
                "division": division.name, "round": 1,
                "player": pairing.first.key,
            })
        response = self._post_json("division_edit_results", division, {"rows": []})
        self.assertEqual(response.status_code, 200)
        self.client.post(reverse("unpublish_round", kwargs=division.slug_kwargs()), {"round": 1})
        self.client.get(reverse("division_pair_rounds", kwargs=division.slug_kwargs()))
        self.client.post(reverse("publish_round", kwargs=division.slug_kwargs()), {"round": 1})
        recorded = division_digest(division)
        ctx = replay(events_from_tournament(tournament), verify=True)
        self.assertEqual(division_digest(ctx.tournament.divisions.get()), recorded)
        self.assertEqual(ctx.drift, [])

    def test_reset_with_played_results_replays_exactly(self):
        from tournaments.commands import reset_round, publish_round
        from tournaments.events import division_digest
        from tournaments.replay import events_from_tournament, replay

        tournament, division = self._build_logged_tournament()
        pairing = division.pairings.filter(round=1).first()
        response = self._post_json("division_edit_results", division, {"rows": [{
            "round": 1, "winner": pairing.first_id, "loser": pairing.second_id,
            "winner_score": 400, "loser_score": 350, "winner_started": True,
        }]})
        self.assertEqual(response.status_code, 200)
        reset_round(tournament, self.owner, {"division": division.name, "round": 1})
        regenerate_pairings(division)
        publish_round(tournament, self.owner, {"division": division.name, "round": 1})
        ctx = replay(events_from_tournament(tournament), verify=True)
        self.assertEqual(division_digest(ctx.tournament.divisions.get()), division_digest(division))
        self.assertEqual(ctx.drift, [])
