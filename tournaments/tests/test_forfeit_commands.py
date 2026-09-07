"""Withdrawing and forfeiting a game that is already printed (PLAN_FORFEITS §4).

A published round cannot be re-paired, so the printed game is dissolved and both
players take a bye-shaped row: the opponent a bye, the absentee a forfeit. The
point of doing it that way is that a withdrawal before pairing and one after it
land on the same shape.
"""

from django.test import TestCase
from django.urls import reverse

from tournaments.forfeits import (
    ForfeitError,
    forfeit_game,
    rejoin_entrant,
    save_absence_settings,
    withdraw_entrant,
)
from tournaments.generate_pairings import publish_rounds, regenerate_pairings
from tournaments.models import (
    DivisionSettings,
    Entrant,
    ResultSlip,
    RoundPairings,
)
from tournaments.tests.test_byes import make_division
from users.models import User


class PrintedRoundBase(TestCase):
    policy = DivisionSettings.FORFEIT

    def setUp(self):
        self.user = User.objects.create_user(username="w", password="p")
        self.division = make_division(self.user, 6, 6)
        self.division.entrants.update(rating=1500, rating_source=Entrant.COCO)
        settings = self.division.settings
        settings.withdrawal = self.policy
        settings.save(update_fields=["withdrawal"])
        self.tournament = self.division.tournament

    def publish(self, round_num):
        regenerate_pairings(self.division)
        publish_rounds(self.division, [round_num])

    def rows(self, round_num):
        return self.division.pairings.filter(round=round_num).select_related(
            "first__player", "second__player"
        )

    def bye_shaped(self, round_num, entrant):
        return self.rows(round_num).filter(
            first=entrant, second__player__is_bye=True
        ).first()


class WithdrawFromAPrintedRoundTests(PrintedRoundBase):
    def test_the_printed_game_is_split_into_a_bye_and_a_forfeit(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        absentee, opponent = pairing.first, pairing.second

        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": absentee.key},
        )

        self.assertFalse(self.division.pairings.filter(pk=pairing.pk).exists())
        forfeit = self.bye_shaped(1, absentee)
        bye = self.bye_shaped(1, opponent)
        self.assertIsNotNone(forfeit)
        self.assertIsNotNone(bye)
        self.assertTrue(forfeit.forfeit)
        self.assertFalse(bye.forfeit)

        forfeit_slip = forfeit.result
        bye_slip = bye.result
        self.assertEqual(forfeit_slip.loser_id, absentee.pk)
        self.assertEqual(forfeit_slip.winner_score, 50)
        self.assertEqual(bye_slip.winner_id, opponent.pk)
        self.assertEqual(bye_slip.winner_score, 50)

    def test_the_opponent_now_counts_as_having_had_a_bye(self):
        # The half decision 4's earlier draft could not deliver: the opponent
        # holds a real bye pairing, so bye avoidance sees it.
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        opponent = pairing.second
        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": pairing.first.key},
        )
        from tournaments.pairing.base import PairingData

        published = PairingData.for_division(self.division).published_pairings[1]
        self.assertIn(("BYE", opponent.key), published)

    def test_a_withdrawal_before_and_after_pairing_agree(self):
        """The whole point of splitting: one shape, not two."""
        before = make_division(self.user, 6, 6, first_number=101)
        s = before.settings
        s.withdrawal = DivisionSettings.FORFEIT
        s.save(update_fields=["withdrawal"])
        victim = before.entrants.order_by("number").first()
        withdraw_entrant(
            before.tournament, self.user,
            {"division": before.name, "player": victim.key},
        )
        regenerate_pairings(before)
        publish_rounds(before, [1])

        self.publish(1)
        after = self.rows(1).exclude(second__player__is_bye=True).first().first
        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": after.key},
        )

        def shape(division, entrant):
            p = division.pairings.filter(
                round=1, first=entrant, second__player__is_bye=True
            ).get()
            slip = p.result
            return (
                p.forfeit, p.table,
                slip.winner.player.is_bye, slip.winner_score,
                slip.loser_score, slip.winner_started, slip.forfeit,
            )

        self.assertEqual(shape(before, victim), shape(self.division, after))

    def test_the_round_can_still_finish(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": pairing.first.key},
        )
        for p in self.rows(1).filter(result__isnull=True):
            ResultSlip.objects.create(
                division=self.division, round=1, pairing=p,
                winner=p.first, winner_score=420,
                loser=p.second, loser_score=380, winner_started=True,
            )
        self.division.round_pairings_set.get(round=1).update_status()
        self.assertEqual(
            self.division.round_pairings_set.get(round=1).status,
            RoundPairings.FINISHED,
        )

    def test_a_played_game_is_refused(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        ResultSlip.objects.create(
            division=self.division, round=1, pairing=pairing,
            winner=pairing.first, winner_score=420,
            loser=pairing.second, loser_score=380, winner_started=True,
        )
        with self.assertRaises(ForfeitError):
            withdraw_entrant(
                self.tournament, self.user,
                {"division": self.division.name, "player": pairing.first.key},
            )

    def test_rejoining_leaves_the_printed_rounds_alone(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        absentee = pairing.first
        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": absentee.key},
        )
        def printed_round():
            return sorted(
                (p.first.key, p.second.key, p.forfeit,
                 getattr(getattr(p, "result", None), "winner_score", None))
                for p in self.rows(1)
            )

        before = printed_round()
        rejoin_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": absentee.key},
        )
        self.assertFalse(self.division.entrants.get(pk=absentee.pk).dropped)
        # The round they missed keeps its forfeit; only the entrant's own flag
        # moves, which the digest does record.
        self.assertEqual(printed_round(), before)


class OmitPolicyTests(PrintedRoundBase):
    policy = DivisionSettings.OMIT

    def test_the_opponent_gets_a_bye_and_the_absentee_gets_nothing(self):
        # "Leave the rounds blank" still has to resolve the opponent's printed
        # game, or the round waits forever on one nobody will play.
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        absentee, opponent = pairing.first, pairing.second
        withdraw_entrant(
            self.tournament, self.user,
            {"division": self.division.name, "player": absentee.key},
        )
        self.assertIsNone(self.bye_shaped(1, absentee))
        self.assertIsNotNone(self.bye_shaped(1, opponent))
        self.assertFalse(
            self.division.result_slips.filter(loser=absentee).exists()
        )


class ForfeitOneGameTests(PrintedRoundBase):
    policy = DivisionSettings.OMIT  # the command ignores the withdrawal rule

    def test_a_single_game_forfeit_needs_no_withdrawal(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        absentee, opponent = pairing.first, pairing.second
        forfeit_game(
            self.tournament, self.user,
            {"division": self.division.name, "round": 1, "player": absentee.key},
        )
        self.assertTrue(self.bye_shaped(1, absentee).forfeit)
        self.assertFalse(self.bye_shaped(1, opponent).forfeit)
        # Not withdrawn: they play on.
        self.assertFalse(self.division.entrants.get(pk=absentee.pk).dropped)

    def test_forfeiting_an_unpublished_round_is_refused(self):
        with self.assertRaises(ForfeitError):
            forfeit_game(
                self.tournament, self.user,
                {
                    "division": self.division.name, "round": 1,
                    "player": self.division.entrants.first().key,
                },
            )

    def test_forfeiting_a_game_the_player_does_not_have_is_refused(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        forfeit_game(
            self.tournament, self.user,
            {"division": self.division.name, "round": 1,
             "player": pairing.first.key},
        )
        with self.assertRaises(ForfeitError):
            forfeit_game(
                self.tournament, self.user,
                {"division": self.division.name, "round": 1,
                 "player": pairing.first.key},
            )


class ForfeitReplayTests(TestCase):
    """A log containing a withdrawal replays to the same division.

    This is what the split had to preserve: the printed rounds it rewrites are
    hashed by ``division_digest``, so the repair has to come out identically
    from the same inputs.
    """

    def test_a_withdrawal_replays_to_the_same_digest(self):
        from tournaments.commands import (
            add_entrant, create_division, create_tournament, publish_round,
            save_settings,
        )
        from tournaments.events import division_digest, export_jsonl
        from tournaments.models import Player, Tournament
        from tournaments.replay import replay, parse_jsonl

        user = User.objects.create_user(username="r", password="p")
        tournament = create_tournament(None, user, {
            "name": "Replay Cup", "location": "x", "start_date": "2026-03-15",
        })
        create_division(tournament, user, {"name": "D"})
        for i in range(1, 7):
            Player.objects.create(name=f"R{i}", player_number=f"9{i:02d}",
                                  rating=1600 - i)
            add_entrant(tournament, user, {
                "division": "D", "player": f"9{i:02d}",
            })
        save_settings(tournament, user, {
            "division": "D",
            "blocks": [{"pairing": "KotH", "rounds": 4, "pair_from": 1}],
        })
        save_absence_settings(tournament, user, {
            "division": "D",
            "withdrawal": DivisionSettings.FORFEIT,
            "bye_spread": 100,
        })
        division = tournament.divisions.get(name="D")
        regenerate_pairings(division)
        publish_round(tournament, user, {"division": "D", "round": 1})
        victim = division.pairings.filter(
            round=1
        ).exclude(second__player__is_bye=True).first().first
        withdraw_entrant(tournament, user, {"division": "D", "player": victim.key})

        expected = division_digest(division)
        self.assertTrue(
            division.pairings.filter(round=1, forfeit=True).exists(),
            "the withdrawal should have left a forfeit row to reproduce",
        )

        jsonl = export_jsonl(tournament)
        Tournament.objects.all().delete()
        _header, events = parse_jsonl(jsonl)
        ctx = replay(events, verify=True)
        rebuilt = ctx.tournament.divisions.get(name="D")
        self.assertEqual(division_digest(rebuilt), expected)
        self.assertEqual(rebuilt.settings.bye_spread, 100)
        self.assertEqual(rebuilt.settings.withdrawal, DivisionSettings.FORFEIT)


class ForfeitSurfaceTests(PrintedRoundBase):
    """The pages that expose all this (PLAN_FORFEITS phase 4)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.slugs = (self.division.tournament.slug, self.division.slug)

    def entrants_page(self):
        return self.client.get(reverse("division_entrants", args=self.slugs))

    def test_withdraw_and_rejoin_from_the_entrants_page(self):
        entrant = self.division.entrants.order_by("number").first()
        withdraw_url = reverse("division_withdraw_entrant", args=self.slugs)
        self.assertContains(self.entrants_page(), withdraw_url)

        response = self.client.post(withdraw_url, {"player": entrant.key})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.division.entrants.get(pk=entrant.pk).dropped)

        # The row now offers the way back instead.
        page = self.entrants_page()
        self.assertContains(page, reverse("division_rejoin_entrant", args=self.slugs))
        self.client.post(
            reverse("division_rejoin_entrant", args=self.slugs),
            {"player": entrant.key},
        )
        self.assertFalse(self.division.entrants.get(pk=entrant.pk).dropped)

    def test_the_public_embed_offers_no_withdraw_control(self):
        # The partial is shared, so this is the check that keeps a director's
        # action off a page anyone can iframe.
        response = self.client.get(
            reverse("division_entrants_embed", args=self.slugs)
        )
        self.assertNotContains(
            response, reverse("division_withdraw_entrant", args=self.slugs)
        )

    def test_a_forfeit_button_appears_on_a_published_game(self):
        self.publish(1)
        response = self.client.get(
            reverse("round_pairings_tab", args=(*self.slugs, 1))
        )
        self.assertContains(response, "forfeits")
        self.assertContains(response, reverse("forfeit_game", args=self.slugs))

    def test_forfeiting_through_the_view(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        absentee, opponent = pairing.first, pairing.second
        response = self.client.post(
            reverse("forfeit_game", args=self.slugs),
            {"round": 1, "player": absentee.key},
        )
        # No-JS post: the shared helper flashes and redirects rather than
        # swapping the pairings body.
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.bye_shaped(1, absentee).forfeit)
        self.assertFalse(self.bye_shaped(1, opponent).forfeit)

    def test_a_forfeit_row_says_forfeit_not_bye(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        self.client.post(
            reverse("forfeit_game", args=self.slugs),
            {"round": 1, "player": pairing.first.key},
        )
        body = self.client.get(
            reverse("round_pairings_tab", args=(*self.slugs, 1))
        ).content.decode()
        self.assertIn("tag-forfeit", body)
        self.assertIn("tag-bye", body)

    def test_the_public_pairings_page_offers_no_slip_for_an_absence(self):
        self.publish(1)
        pairing = self.rows(1).exclude(second__player__is_bye=True).first()
        self.client.post(
            reverse("forfeit_game", args=self.slugs),
            {"round": 1, "player": pairing.first.key},
        )
        body = self.client.get(
            reverse("division_pairings", args=self.slugs)
        ).content.decode()
        self.assertIn("tag-forfeit", body)

    def test_the_settings_page_saves_the_absence_rule(self):
        url = reverse("division_settings", args=self.slugs)
        self.assertContains(self.client.get(url), "bye_spread")
        response = self.client.post(
            url,
            {"absence": "1", "withdrawal": DivisionSettings.OMIT, "bye_spread": 100},
        )
        self.assertEqual(response.status_code, 302)
        settings = self.division.settings
        settings.refresh_from_db()
        self.assertEqual(settings.withdrawal, DivisionSettings.OMIT)
        self.assertEqual(settings.bye_spread, 100)

    def test_saving_the_absence_rule_does_not_touch_the_schedule(self):
        # Its own command precisely so a settings form never has to send a
        # block list it could get wrong.
        before = self.division.settings.pairing_blocks
        self.client.post(
            reverse("division_settings", args=self.slugs),
            {"absence": "1", "withdrawal": DivisionSettings.FORFEIT,
             "bye_spread": 75},
        )
        settings = self.division.settings
        settings.refresh_from_db()
        self.assertEqual(settings.pairing_blocks, before)
