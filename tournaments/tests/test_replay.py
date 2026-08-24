"""Phase 4 event-log tests: replaying a recorded tournament reproduces it."""

import json

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.events import division_digest
from tournaments.models import Player, Tournament
from tournaments.replay import events_from_tournament, replay
from users.models import User


class LoggedTournamentMixin:
    """Builds a small tournament through the real views, so every step logs."""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.players = [
            Player.objects.create(name=n, player_number=f"00{i}", rating=r)
            for i, (n, r) in enumerate(
                [("Alice", 1600), ("Bob", 1500), ("Cara", 1400), ("Dan", 1300)], 1
            )
        ]
        self.client.force_login(self.owner)

    def _post_json(self, name, division, body):
        return self.client.post(
            reverse(name, kwargs=division.slug_kwargs()),
            json.dumps(body),
            content_type="application/json",
        )

    def _build_logged_tournament(self, players=None):
        """Drive a small tournament through the real views so every step logs."""
        self.client.post(
            reverse("tournament_create"),
            {
                "name": "Champs",
                "location": "Reno",
                "start_date": "2026-03-15",
                "editor_usernames": "",
            },
        )
        tournament = Tournament.objects.get(name="Champs")
        division = tournament.divisions.get()  # default "Division 1"

        self._post_json(
            "division_entrants_edit",
            division,
            {
                "rows": [
                    {"number": i + 1, "player": p.pk, "dropped": False}
                    for i, p in enumerate(players or self.players)
                ]
            },
        )
        self._post_json(
            "division_round_pairings",
            division,
            {"blocks": [{"pairing": "KotH", "rounds": 2, "pair_from": 1}]},
        )
        # A Pair Rounds render lazily regenerates the draft pairings, as in real use.
        self.client.get(reverse("division_pair_rounds", kwargs=division.slug_kwargs()))
        # publish reads form/datastar data (not a JSON body).
        self.client.post(
            reverse("publish_round", kwargs=division.slug_kwargs()), {"round": 1}
        )
        return tournament, division


@tag("slow")
class ReplayTests(LoggedTournamentMixin, TestCase):
    def test_replay_reproduces_digest_end_to_end(self):
        tournament, division = self._build_logged_tournament()
        recorded_digest = division_digest(division)
        events = events_from_tournament(tournament)
        self.assertTrue(events)

        # Replay into the same DB creates a fresh tournament; verify every event's
        # recorded digest matches the replayed state.
        ctx = replay(events, verify=True)

        self.assertNotEqual(ctx.tournament.pk, tournament.pk)
        replayed_division = ctx.tournament.divisions.get()
        self.assertEqual(division_digest(replayed_division), recorded_digest)
        # The published round came back with its pairings.
        self.assertEqual(
            replayed_division.round_pairings_set.get(round=1).status,
            division.round_pairings_set.get(round=1).status,
        )

    def test_replay_from_jsonl_export(self):
        from tournaments.events import export_jsonl
        from tournaments.replay import parse_jsonl

        tournament, division = self._build_logged_tournament()
        header, events = parse_jsonl(export_jsonl(tournament))
        self.assertEqual(header["kind"], "header")
        ctx = replay(events, verify=True)
        self.assertEqual(
            division_digest(ctx.tournament.divisions.get()), division_digest(division)
        )

    def test_snapshot_of_prelog_tournament_replays(self):
        from datetime import date

        from tournaments.events import snapshot_existing
        from tournaments.models import (
            Division,
            Entrant,
            Pairing,
            ResultSlip,
            RoundPairings,
            Tournament,
        )

        # A "pre-existing" tournament built directly — no event log.
        t = Tournament.objects.create(
            name="Legacy", location="X", start_date=date(2026, 3, 15), owner=self.owner
        )
        t.editors.add(self.owner)
        div = Division.objects.create(name="Open", tournament=t)
        e1 = Entrant.objects.create(division=div, player=self.players[0], number=1)
        e2 = Entrant.objects.create(division=div, player=self.players[1], number=2)
        rp = RoundPairings.objects.create(
            division=div, round=1, status=RoundPairings.FINISHED
        )
        pairing = Pairing.objects.create(
            division=div, round=1, round_pairings=rp, first=e1, second=e2, table=1
        )
        ResultSlip.objects.create(
            division=div, round=1, pairing=pairing, winner=e1, winner_score=450,
            loser=e2, loser_score=380, winner_started=True,
        )
        recorded = division_digest(div)

        event = snapshot_existing(t)
        self.assertEqual(event.event_type, "state_snapshot")
        self.assertIsNone(snapshot_existing(t))  # idempotent — already has a log

        ctx = replay(events_from_tournament(t))
        self.assertEqual(division_digest(ctx.tournament.divisions.get()), recorded)

    def test_upto_stops_after_prefix(self):
        tournament, division = self._build_logged_tournament()
        events = events_from_tournament(tournament)
        # Stop right after the tournament is created (its default division exists,
        # but no entrants have been added yet).
        created = next(e for e in events if e["event_type"] == "tournament_created")
        ctx = replay(events, upto=created["seq"])
        replayed = ctx.tournament.divisions.get()
        self.assertEqual(replayed.entrants.count(), 0)


@tag("slow")
class PublishedStartCorrectionTests(LoggedTournamentMixin, TestCase):
    """The published board owns the start. A result grid row that says otherwise
    is rewritten to match, and the rewrite is itself a logged event."""

    def _enter_result(self, division, pairing, winner, winner_started):
        loser = pairing.second if winner == pairing.first else pairing.first
        return self._post_json(
            "division_edit_results",
            division,
            {
                "rows": [
                    {
                        "round": pairing.round,
                        "winner": winner.pk,
                        "winner_score": 450,
                        "loser": loser.pk,
                        "loser_score": 380,
                        "winner_started": winner_started,
                    }
                ]
            },
        )

    def _event_types(self, tournament):
        return [e.event_type for e in tournament.events.order_by("seq")]

    def test_start_entered_against_the_board_is_rewritten(self):
        tournament, division = self._build_logged_tournament()
        pairing = division.pairings.filter(round=1).order_by("table").first()
        # The board says `first` started; enter the result claiming they did not.
        response = self._enter_result(division, pairing, pairing.first, False)
        self.assertEqual(response.status_code, 200)

        slip = division.result_slips.get()
        self.assertTrue(slip.winner_started, "the published start should have won")

    def test_the_rewrite_is_logged_after_the_save_that_caused_it(self):
        tournament, division = self._build_logged_tournament()
        pairing = division.pairings.filter(round=1).order_by("table").first()
        self._enter_result(division, pairing, pairing.first, False)

        self.assertEqual(
            self._event_types(tournament)[-2:],
            ["results_saved", "result_starts_corrected"],
        )
        correction = tournament.events.order_by("seq").last()
        self.assertEqual(
            correction.payload["corrections"],
            [
                {
                    "round": 1,
                    "winner": pairing.first.player.name,
                    "loser": pairing.second.player.name,
                    "winner_started": True,
                }
            ],
        )
        # The save event keeps what was actually entered, so the log shows the
        # wrong start and then its correction rather than quietly rewriting
        # history.
        saved = tournament.events.filter(event_type="results_saved").last()
        self.assertEqual(saved.payload["rows"][0]["winner_started"], False)

    def test_a_start_that_matches_the_board_logs_no_correction(self):
        tournament, division = self._build_logged_tournament()
        pairing = division.pairings.filter(round=1).order_by("table").first()
        self._enter_result(division, pairing, pairing.first, True)

        self.assertNotIn("result_starts_corrected", self._event_types(tournament))
        self.assertTrue(division.result_slips.get().winner_started)

    def test_the_corrected_log_replays_to_the_same_state(self):
        tournament, division = self._build_logged_tournament()
        pairing = division.pairings.filter(round=1).order_by("table").first()
        self._enter_result(division, pairing, pairing.first, False)
        recorded = division_digest(division)

        ctx = replay(events_from_tournament(tournament), verify=True)

        self.assertEqual(division_digest(ctx.tournament.divisions.get()), recorded)

    def test_a_bye_is_never_corrected(self):
        # A bye row is stored real-player-first for display, which contradicts
        # its slip by construction (the bye opponent is the notional starter).
        # Comparing the two would "correct" every bye into charging its player.
        from tournaments.starts import start_conflicts

        eve = Player.objects.create(name="Eve", player_number="005", rating=1200)
        tournament, division = self._build_logged_tournament(self.players + [eve])
        bye = division.pairings.get(round=1, second__player__is_bye=True)
        slip = division.result_slips.get(pairing=bye)
        self.assertEqual(slip.winner, bye.first)
        self.assertFalse(slip.winner_started)

        self.assertEqual(start_conflicts(division), [])

    def test_a_draft_round_is_not_authoritative(self):
        # Round 2 was never published, so its pairing is not a promise to anyone
        # and a result entered against it keeps the start it was given.
        from tournaments.starts import start_conflicts
        from tournaments.models import Pairing, ResultSlip, RoundPairings

        tournament, division = self._build_logged_tournament()
        rp = RoundPairings.objects.create(
            division=division, round=2, status=RoundPairings.DRAFT
        )
        entrants = list(division.entrants.order_by("number")[:2])
        pairing = Pairing.objects.create(
            division=division, round=2, round_pairings=rp,
            first=entrants[0], second=entrants[1], table=1,
        )
        ResultSlip.objects.create(
            division=division, round=2, pairing=pairing,
            winner=entrants[0], winner_score=450,
            loser=entrants[1], loser_score=380, winner_started=False,
        )

        self.assertEqual(start_conflicts(division), [])


@tag("slow")
class V1PayloadUpgradeTests(TestCase):
    """A log written before player numbers were the identity still replays.

    The fixture is written out by hand rather than downgraded from a fresh
    export: a downgrade helper would be the inverse of the upgraders under test,
    so a matching mistake in both would cancel out and the test would pass while
    proving nothing.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")

    V1_EVENTS = [
        {
            "seq": 1,
            "actor": "owner",
            "division": None,
            "event_type": "tournament_created",
            "schema_version": 1,
            "payload": {
                "name": "Legacy Open",
                "location": "Reno",
                "start_date": "2026-03-15",
                "editors": [],
                "default_division": {"name": "Division 1", "pairing_seed": 7},
            },
        },
        {
            "seq": 2,
            "actor": "owner",
            "division": "Division 1",
            "event_type": "entrants_saved",
            "schema_version": 1,
            # A v1 entrant row names the player and carries no number at all.
            "payload": {
                "division": "Division 1",
                "rows": [
                    {"number": 1, "player": "Alice", "rating": 1600, "dropped": False},
                    {"number": 2, "player": "Bob", "rating": 1500, "dropped": False},
                    {"number": 3, "player": "Cara", "rating": 1400, "dropped": False},
                    {"number": 4, "player": "Dan", "rating": 1300, "dropped": False},
                ],
            },
        },
        {
            "seq": 3,
            "actor": "owner",
            "division": "Division 1",
            "event_type": "division_settings_saved",
            "schema_version": 1,
            "payload": {
                "division": "Division 1",
                "blocks": [{"pairing": "KotH", "rounds": 2, "pair_from": 1}],
            },
        },
        {
            "seq": 4,
            "actor": "owner",
            "division": "Division 1",
            "event_type": "round_published",
            "schema_version": 1,
            "payload": {"division": "Division 1", "round": 1},
        },
        {
            "seq": 5,
            "actor": "owner",
            "division": "Division 1",
            "event_type": "result_added",
            "schema_version": 1,
            # v1 spelling: first_name / second_name / winner_name.
            "payload": {
                "division": "Division 1",
                "round": 1,
                "first_name": "Alice",
                "second_name": "Bob",
                "winner_name": "Alice",
                "winner_score": 450,
                "loser_score": 380,
            },
        },
        {
            "seq": 6,
            "actor": "owner",
            "division": "Division 1",
            "event_type": "fixed_pairing_added",
            "schema_version": 1,
            # v1 spelling: name1 / name2.
            "payload": {
                "division": "Division 1",
                "round": 2,
                "name1": "Alice",
                "name2": "Dan",
            },
        },
    ]

    def test_a_v1_log_replays_through_the_upgraders(self):
        ctx = replay([dict(e) for e in self.V1_EVENTS])
        division = ctx.tournament.divisions.get()

        # The roster came back, with locally-minted numbers (v1 logs carry none).
        self.assertEqual(division.entrants.count(), 4)
        names = set(division.entrants.values_list("player__name", flat=True))
        self.assertEqual(names, {"Alice", "Bob", "Cara", "Dan"})
        for entrant in division.entrants.select_related("player"):
            self.assertTrue(entrant.key.startswith("T-"), entrant.key)

        # The v1 result found its pairing by name and was written against the
        # right two people.
        slip = division.result_slips.get()
        self.assertEqual(slip.round, 1)
        self.assertEqual(slip.winner.player.name, "Alice")
        self.assertEqual(slip.loser.player.name, "Bob")
        self.assertEqual((slip.winner_score, slip.loser_score), (450, 380))

        # …and so did the v1 fixed pairing.
        fp = division.fixed_pairings.get()
        self.assertEqual(fp.round_number, 2)
        self.assertEqual(
            {fp.entrant1.player.name, fp.entrant2.player.name}, {"Alice", "Dan"}
        )

    def test_a_v1_result_is_not_applied_by_name(self):
        """The upgrade resolves to a number; the command never sees the name."""
        from tournaments.replay import SCHEMA_UPGRADES

        replay([dict(e) for e in self.V1_EVENTS[:4]])
        upgraded = SCHEMA_UPGRADES["result_added"](self.V1_EVENTS[4]["payload"], 1)
        self.assertNotIn("winner_name", upgraded)
        alice = Player.objects.get(name="Alice")
        self.assertEqual(upgraded["winner_player"], alice.player_number)
        self.assertEqual(upgraded["first_player"], alice.player_number)

    def test_a_v2_payload_passes_through_untouched(self):
        from tournaments.replay import SCHEMA_UPGRADES

        payload = {"division": "D", "round": 1, "first_player": "0001"}
        self.assertIs(SCHEMA_UPGRADES["result_added"](payload, 2), payload)

    def test_kept_fixed_pairings_are_resorted_after_the_rename(self):
        """Name order and number order are unrelated, and the consumer sorts."""
        from tournaments.replay import SCHEMA_UPGRADES

        # Zoe gets the lower number, so the pair sorts the other way once
        # rekeyed — the case a rename alone would get wrong.
        Player.objects.create(name="Zoe", player_number="0001", rating=1500)
        Player.objects.create(name="Abe", player_number="0002", rating=1400)
        upgraded = SCHEMA_UPGRADES["fixed_pairings_removed"](
            {"division": "D", "kept": [[3, "Abe", "Zoe"]]}, 1
        )
        self.assertEqual(upgraded["kept"], [[3, "0001", "0002"]])
