"""A draft board must not outlive the schedule or config it was paired under."""

from django.test import TestCase

from tournaments.commands import save_cop_config, save_settings, unpublish_round
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import Division, RoundPairings
from tournaments.pairings_view import PairingsPresenter
from tournaments.tests.test_byes import make_division
from users.models import User


def board(division, round_num):
    return sorted(
        sorted([p.first.player.player_number, p.second.player.player_number])
        for p in division.pairings.filter(round=round_num).select_related(
            "first__player", "second__player"
        )
    )


class UnpublishAfterScheduleChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="d", password="p")
        self.division = make_division(self.user, 8, 3, pairing="KotH")
        self.tournament = self.division.tournament
        regenerate_pairings(self.division)
        publish_rounds(self.division, [1])
        self.printed = board(self.division, 1)

    def unpublish(self):
        unpublish_round(self.tournament, self.user, {"division": "D", "round": 1})
        # What the Pair Rounds render does, on a fresh division as a request
        # would have (this one caches the settings from before any save).
        self.division = Division.objects.get(pk=self.division.pk)
        if PairingsPresenter(self.division).rounds_needing_generation:
            regenerate_pairings(self.division)

    def test_the_board_records_what_paired_it(self):
        rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual((rp.paired_with, rp.paired_from), ("KotH", 0))

    def test_an_unchanged_round_comes_back_as_printed(self):
        self.unpublish()
        self.assertEqual(board(self.division, 1), self.printed)

    def test_a_round_whose_strategy_changed_is_drawn_again(self):
        # Changed while published: the save leaves the printed board alone.
        save_settings(self.tournament, self.user, {
            "division": "D",
            "blocks": [{"pairing": "SplitSwiss", "rounds": 3, "pair_from": 1}],
        })
        self.assertEqual(board(self.division, 1), self.printed)
        self.unpublish()
        rp = self.division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        self.assertEqual(rp.paired_with, "SplitSwiss")
        # Split Swiss from seedings: top half against bottom half.
        self.assertEqual(board(self.division, 1), [["0001", "0005"], ["0002", "0006"], ["0003", "0007"], ["0004", "0008"]])


class CopConfigDropsDraftsTests(TestCase):
    def test_saving_the_cop_config_drops_drafts(self):
        user = User.objects.create_user(username="d", password="p")
        division = make_division(user, 8, 3, pairing="KotH")
        regenerate_pairings(division)
        self.assertTrue(division.round_pairings_set.filter(status=RoundPairings.DRAFT).exists())
        save_cop_config(division.tournament, user, {
            "division": "D", "cop_config": {"place_prizes": 2},
        })
        self.assertFalse(division.round_pairings_set.filter(status=RoundPairings.DRAFT).exists())
