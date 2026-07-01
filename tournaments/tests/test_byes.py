"""Bye handling: a synthetic bye is added at pairing time for an odd field."""

from collections import Counter
from datetime import date

from django.test import TestCase

from tournaments.fake_tournament import create_fake_tournament
from tournaments.generate_pairings import (
    BYE_WINNER_SCORE,
    publish_rounds,
    regenerate_pairings,
    unpublish_rounds,
)
from tournaments.match_simulation import simulate_round
from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.pairing.base import PairingData, standings_after_round
from tournaments.pairing.round_pairing import blocks_to_round_pairings
from tournaments.tournament_export import ExportTournament
from users.models import User


def make_division(owner, n_players, rounds, pairing="KotH"):
    tournament = Tournament.objects.create(
        name="T", location="x", start_date=date.today(), owner=owner
    )
    division = Division.objects.create(tournament=tournament, name="D")
    for i in range(1, n_players + 1):
        Entrant.objects.create(
            division=division,
            player=Player.objects.create(
                name=f"P{i}", player_number=str(i), rating=2000 - i
            ),
            number=i,
        )
    blocks = [{"pairing": pairing, "rounds": rounds, "pair_from": 1}]
    DivisionSettings.objects.create(
        division=division,
        pairing_blocks=blocks,
        round_pairings=[rp.to_dict() for rp in blocks_to_round_pairings(blocks)],
    )
    return division


class ByePlayerModelTests(TestCase):
    def test_get_bye_is_a_singleton(self):
        first = Player.get_bye()
        again = Player.get_bye()
        self.assertEqual(first.pk, again.pk)
        self.assertTrue(first.is_bye)
        self.assertTrue(first.is_provisional)
        self.assertEqual(Player.objects.filter(is_bye=True).count(), 1)

    def test_default_manager_hides_bye_entrant(self):
        user = User.objects.create_user(username="o", password="p")
        division = make_division(user, 3, 2)
        bye_entrant = division.bye_entrant()

        # Reverse relation (and the default manager) exclude the bye...
        self.assertEqual(division.entrants.count(), 3)
        self.assertNotIn(
            bye_entrant.pk, division.entrants.values_list("pk", flat=True)
        )
        # ...but it is reachable via all_objects.
        self.assertIn(
            bye_entrant.pk,
            Entrant.all_objects.filter(division=division).values_list("pk", flat=True),
        )


class ByePairingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="byeowner", password="p")

    def test_odd_field_creates_a_bye_pairing(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)

        round1 = list(division.pairings.filter(round=1))
        self.assertEqual(len(round1), 3)  # 2 real games + 1 bye
        byes = [
            p for p in round1 if p.first.player.is_bye or p.second.player.is_bye
        ]
        self.assertEqual(len(byes), 1)
        self.assertEqual(byes[0].table, 0)
        # No result until the round is published.
        self.assertEqual(division.result_slips.filter(round=1).count(), 0)

    def test_publishing_records_the_bye_as_a_win(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])

        slip = division.result_slips.get(round=1, loser__player__is_bye=True)
        self.assertFalse(slip.winner.player.is_bye)
        self.assertEqual(slip.winner_score - slip.loser_score, BYE_WINNER_SCORE)
        # The byed player is the notional non-starter (bye "starts").
        self.assertFalse(slip.winner_started)

    def test_publishing_an_odd_round_stays_published_not_in_progress(self):
        # The auto bye isn't a played game, so a freshly published odd round is
        # PUBLISHED, not IN_PROGRESS — only a real result moves it forward.
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.PUBLISHED)

    def test_first_real_result_moves_odd_round_to_in_progress(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        real = [
            p for p in division.pairings.filter(round=1)
            if not (p.first.player.is_bye or p.second.player.is_bye)
        ][0]
        ResultSlip.objects.create(
            division=division, round=1, pairing=real,
            winner=real.first, winner_score=450,
            loser=real.second, loser_score=380, winner_started=True,
        )
        real.round_pairings.update_status()
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.IN_PROGRESS)

    def test_unpublishing_clears_the_bye_and_reverts_to_draft(self):
        # The auto bye isn't a real result, so the round can still be unpublished
        # — and its bye slip is dropped for a clean draft.
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        self.assertTrue(
            division.result_slips.filter(round=1, loser__player__is_bye=True).exists()
        )

        unpublished = unpublish_rounds(division, [1])

        self.assertEqual(unpublished, [1])
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.DRAFT)
        self.assertFalse(division.result_slips.filter(round=1).exists())
        # Pairings survive so the round can be edited and republished.
        self.assertTrue(rp.pairings.exists())

    def test_unpublishing_blocked_by_a_real_result(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        # Enter a real game result alongside the auto bye.
        real = [
            p for p in division.pairings.filter(round=1)
            if not (p.first.player.is_bye or p.second.player.is_bye)
        ][0]
        ResultSlip.objects.create(
            division=division, round=1, pairing=real,
            winner=real.first, winner_score=450,
            loser=real.second, loser_score=380, winner_started=True,
        )

        self.assertEqual(unpublish_rounds(division, [1]), [])
        # Round and both slips are untouched.
        self.assertTrue(
            division.result_slips.filter(round=1, loser__player__is_bye=True).exists()
        )

    def test_even_field_has_no_bye(self):
        division = make_division(self.user, 6, 2)
        regenerate_pairings(division)
        byes = [
            p
            for p in division.pairings.filter(round=1)
            if p.first.player.is_bye or p.second.player.is_bye
        ]
        self.assertEqual(byes, [])

    def test_standings_exclude_the_bye(self):
        division = make_division(self.user, 5, 3)
        regenerate_pairings(division)
        publish_rounds(division, [1])
        simulate_round(division, 1)

        pd = PairingData.for_division(division)
        standings = standings_after_round(pd, 1)
        self.assertEqual(len(standings), 5)  # all real players, no Bye
        self.assertFalse(any(p.is_bye for p in standings))


class ByeRotationTests(TestCase):
    def test_no_player_gets_a_second_bye_until_all_have_one(self):
        user = User.objects.create_user(username="rot", password="p")
        for i in range(7):
            Player.objects.create(
                name=f"R{i}", player_number=f"R{i}", rating=1500 + i,
                is_provisional=False,
            )
        # 7 players, simulate 5 rounds (5 byes) — fewer byes than players.
        division = create_fake_tournament(user, num_players=7, num_rounds=6)

        bye_counts = Counter(
            slip.winner.name
            for slip in ResultSlip.objects.filter(
                division=division, loser__player__is_bye=True
            )
        )
        self.assertEqual(sum(bye_counts.values()), 5)
        self.assertTrue(all(count == 1 for count in bye_counts.values()))


class ByeExportTests(TestCase):
    def test_export_omits_the_bye_player_and_results(self):
        user = User.objects.create_user(username="exp", password="p")
        for i in range(5):
            Player.objects.create(
                name=f"E{i}", player_number=f"E{i}", rating=1500 + i,
                is_provisional=False,
            )
        division = create_fake_tournament(user, num_players=5, num_rounds=3)

        data = ExportTournament.from_db(division.tournament)
        self.assertNotIn("Bye", [p.name for p in data.players])
        exported = sum(len(d.results) for d in data.divisions)
        real = division.result_slips.exclude(loser__player__is_bye=True).count()
        self.assertEqual(exported, real)
