"""Publishing ahead must not hide or close unfinished earlier games."""
from django.test import TestCase
from django.urls import reverse
from tournaments.generate_pairings import regenerate_pairings, publish_rounds
from tournaments.models import ResultSlip
from tournaments.tests.test_byes import make_division
from users.models import User


class EarlierRoundResultsTests(TestCase):
    def test_anonymous_player_can_submit_round_one_after_round_two_is_published(self):
        owner = User.objects.create_user(username="director", password="pw")
        division = make_division(owner, 4, 3, pairing="RoundRobin")
        regenerate_pairings(division)
        publish_rounds(division, [1])
        first = division.pairings.filter(round=1).first()
        publish_rounds(division, [2])
        url = reverse("resultslip_create", kwargs=division.slug_kwargs())
        for route in ("division_pairings", "published_pairings"):
            response = self.client.get(reverse(route, kwargs=division.slug_kwargs()))
            self.assertContains(response, f'{url}?pairing={first.pk}')
            self.assertEqual(response.context["current_round"], 1)
            self.assertEqual([r for r, _ in response.context["pairings"]], [1, 2])
        form = self.client.get(url, {"pairing": first.pk})
        self.assertEqual(int(form.context["form"].initial["round"]), 1)
        response = self.client.post(url, {
            "round": 1, "pairing": first.pk, "winner": first.first_id,
            "winner_score": 400, "loser_score": 350, "verified_by_opponent": "on",
        })
        self.assertIn(response.status_code, (200, 302))
        result = ResultSlip.objects.get(pairing=first)
        self.assertEqual(result.round, 1)
        self.assertEqual(result.winner_score, 400)
        self.assertFalse(division.result_slips.filter(round=2).exists())
