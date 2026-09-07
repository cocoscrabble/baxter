"""A withdrawal from a block that lays out several rounds at once.

A round robin is a rotation over a fixed field and a quad block is a grouping
into fours: both are decided when the block begins and belong to the players who
began it. Losing one of them does not re-cut the template — the engine is handed
the field that started the block and Baxter dissolves the withdrawn player's
remaining fixtures on the way back (plans/PLAN_FORFEITS.md §7).
"""

from datetime import date

from django.test import TestCase

from tournaments.forfeits import withdraw_entrant
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from tournaments.pairing.round_pairing import blocks_to_round_pairings
from users.models import User


class CommittedBlockBase(TestCase):
    def build(self, blocks, policy, tag, n=6):
        user = User.objects.create_user(username=f"u{tag}", password="p")
        tournament = Tournament.objects.create(
            name="T", location="x", start_date=date.today(), owner=user
        )
        tournament.editors.add(user)
        division = Division.objects.create(tournament=tournament, name="D")
        for i in range(1, n + 1):
            Entrant.objects.create(
                division=division, number=i, rating=1600 - i,
                player=Player.objects.create(
                    name=f"P{i}", player_number=f"{tag}{i:02d}", rating=1600 - i
                ),
            )
        DivisionSettings.objects.create(
            division=division, pairing_blocks=blocks, withdrawal=policy,
            round_pairings=[rp.to_dict() for rp in blocks_to_round_pairings(blocks)],
        )
        return division, user

    def play(self, division, round_num):
        regenerate_pairings(division)
        publish_rounds(division, [round_num])
        for p in division.pairings.filter(round=round_num, result__isnull=True):
            ResultSlip.objects.create(
                division=division, round=round_num, pairing=p,
                winner=p.first, winner_score=420,
                loser=p.second, loser_score=380, winner_started=True,
            )
        division.round_pairings_set.get(round=round_num).update_status()

    def withdraw(self, division, user, name):
        withdraw_entrant(
            division.tournament, user,
            {"division": division.name,
             "player": division.entrants.get(player__name=name).key},
        )

    def rows(self, division, round_num):
        return sorted(
            (p.first.player.name, p.second.player.name, p.forfeit)
            for p in division.pairings.filter(round=round_num)
        )

    def real_meetings(self, division):
        met = {}
        for p in division.pairings.all():
            if p.first.player.is_bye or p.second.player.is_bye:
                continue
            key = frozenset({p.first.player.name, p.second.player.name})
            met[key] = met.get(key, 0) + 1
        return met


class RoundRobinWithdrawalTests(CommittedBlockBase):
    def test_the_survivors_still_meet_each_other_exactly_once(self):
        # The property that makes it a round robin at all, and the one the old
        # behaviour could not keep: re-cutting the template for five players
        # would have rewritten everyone's remaining fixtures.
        division, user = self.build(
            [{"pairing": "RoundRobin", "rounds": 5, "pair_from": 1}],
            DivisionSettings.FORFEIT, "a",
        )
        for r in (1, 2):
            self.play(division, r)
        self.withdraw(division, user, "P1")
        for r in (3, 4, 5):
            self.play(division, r)

        met = self.real_meetings(division)
        survivors = [f"P{i}" for i in range(2, 7)]
        for i, a in enumerate(survivors):
            for b in survivors[i + 1:]:
                self.assertEqual(
                    met.get(frozenset({a, b})), 1, f"{a} vs {b}"
                )

    def test_the_withdrawn_players_fixtures_become_forfeits(self):
        division, user = self.build(
            [{"pairing": "RoundRobin", "rounds": 5, "pair_from": 1}],
            DivisionSettings.FORFEIT, "b",
        )
        for r in (1, 2):
            self.play(division, r)
        self.withdraw(division, user, "P1")
        for r in (3, 4, 5):
            self.play(division, r)

        for r in (3, 4, 5):
            forfeit = division.pairings.get(round=r, forfeit=True)
            self.assertEqual(forfeit.first.player.name, "P1")
            self.assertTrue(forfeit.second.player.is_bye)
            self.assertEqual(forfeit.result.loser_id, forfeit.first_id)
            # ...and the player who would have faced them takes a bye instead.
            byes = division.pairings.filter(
                round=r, forfeit=False, second__player__is_bye=True
            )
            self.assertEqual(byes.count(), 1)

    def test_an_odd_field_still_gives_everyone_exactly_one_row_a_round(self):
        """An odd field byes somebody every round, so a round can legitimately
        carry *two* byes once a withdrawal lands: the rotation's own, and the one
        the absence creates. What must not happen either way is a player
        appearing twice, or not at all — including the round where the rotation
        hands the bye to the withdrawn player, which is theirs to forfeit rather
        than a bye anybody else gains."""
        division, user = self.build(
            [{"pairing": "RoundRobin", "rounds": 5, "pair_from": 1}],
            DivisionSettings.FORFEIT, "c", n=5,
        )
        self.play(division, 1)
        self.withdraw(division, user, "P3")
        for r in (2, 3, 4, 5):
            self.play(division, r)

        everyone = {e.player.name for e in division.entrants.all()}
        for r in (2, 3, 4, 5):
            appearances = []
            for p in division.pairings.filter(round=r):
                appearances += [
                    e.player.name
                    for e in (p.first, p.second)
                    if not e.player.is_bye
                ]
            self.assertEqual(
                sorted(appearances), sorted(everyone), f"round {r}"
            )
            self.assertEqual(
                division.pairings.filter(round=r, forfeit=True).count(), 1,
                f"round {r}",
            )

    def test_a_block_that_has_not_started_is_solved_for_who_is_left(self):
        # Nothing is committed yet, so the better tournament is a complete round
        # robin over the remaining field rather than one with a phantom in it.
        division, user = self.build(
            [{"pairing": "KotH", "rounds": 2, "pair_from": 1},
             {"pairing": "RoundRobin", "rounds": 4, "pair_from": 1}],
            DivisionSettings.FORFEIT, "d",
        )
        for r in (1, 2):
            self.play(division, r)
        self.withdraw(division, user, "P1")
        for r in (3, 4, 5, 6):
            self.play(division, r)
        met = self.real_meetings(division)
        survivors = [f"P{i}" for i in range(2, 7)]
        for i, a in enumerate(survivors):
            for b in survivors[i + 1:]:
                self.assertGreaterEqual(met.get(frozenset({a, b}), 0), 1)

    def test_omit_leaves_the_absentee_out_and_still_finishes(self):
        division, user = self.build(
            [{"pairing": "RoundRobin", "rounds": 5, "pair_from": 1}],
            DivisionSettings.OMIT, "e",
        )
        for r in (1, 2):
            self.play(division, r)
        self.withdraw(division, user, "P1")
        for r in (3, 4, 5):
            self.play(division, r)
        for r in (3, 4, 5):
            self.assertFalse(
                division.pairings.filter(round=r, forfeit=True).exists()
            )
            self.assertEqual(
                division.round_pairings_set.get(round=r).status,
                RoundPairings.FINISHED,
                f"round {r}",
            )


class QuadBlockWithdrawalTests(CommittedBlockBase):
    def test_the_quads_keep_their_grouping(self):
        division, user = self.build(
            [{"pairing": "Quads_Clustered", "rounds": 3, "pair_from": 1}],
            DivisionSettings.FORFEIT, "q", n=8,
        )
        self.play(division, 1)
        self.withdraw(division, user, "P1")
        for r in (2, 3):
            self.play(division, r)

        # The lower quad never met the withdrawal at all: its three rounds are
        # the same games they would have been.
        lower = {"P5", "P6", "P7", "P8"}
        met = self.real_meetings(division)
        for a in sorted(lower):
            for b in sorted(lower):
                if a < b:
                    self.assertEqual(met.get(frozenset({a, b})), 1, f"{a} vs {b}")
        # And the upper quad's survivors never leave it.
        upper = {"P2", "P3", "P4"}
        for p in division.pairings.all():
            names = {p.first.player.name, p.second.player.name}
            if names & upper and not (names & {"Bye"}):
                self.assertFalse(names & lower, f"quads crossed: {names}")
