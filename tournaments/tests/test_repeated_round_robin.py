"""Two full rotations keep separate block boundaries throughout pairing."""

from django.test import TestCase

from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import ResultSlip
from tournaments.pairing.base import PairingData
from tournaments.pairing.round_pairing import blocks_to_round_pairings
from tournaments.tests.test_byes import make_division
from users.models import User


class RepeatedRoundRobinTests(TestCase):
    def test_second_rotation_keeps_first_rotation_order_after_results_change_rankings(self):
        owner = User.objects.create_user(username="rr", password="pw")
        for n in (5, 6):
            with self.subTest(players=n):
                length = n if n % 2 else n - 1
                division = make_division(owner, n, length * 2, pairing="RoundRobin", first_number=n*100)
                settings = division.settings
                settings.pairing_blocks = [
                    {"pairing": "RoundRobin", "rounds": length, "pair_from": 1},
                    {"pairing": "RoundRobin", "rounds": length, "pair_from": 1},
                ]
                settings.round_pairings = [r.to_dict() for r in blocks_to_round_pairings(settings.pairing_blocks)]
                settings.save()
                self.assertEqual([r.start_round for r in PairingData.for_division(division).round_pairings], [1]*length+[length+1]*length)
                regenerate_pairings(division)
                def opponents(r):
                    return {frozenset((p.first.key, p.second.key)) for p in division.pairings.filter(round=r).select_related("first__player", "second__player")}
                first = [opponents(r) for r in range(1, length+1)]
                for r in range(1, length+1):
                    publish_rounds(division, [r])
                    for p in division.pairings.filter(round=r, result__isnull=True):
                        ResultSlip.objects.create(division=division, round=r, pairing=p, winner=p.second, loser=p.first, winner_score=500, loser_score=100, winner_started=False)
                    division.round_pairings_set.get(round=r).update_status()
                regenerate_pairings(division)
                for r in range(1, length+1):
                    self.assertEqual(opponents(r+length), first[r-1])
                    self.assertEqual(len(opponents(r+length)), (n+1)//2)
