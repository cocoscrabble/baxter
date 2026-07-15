"""Phase 4 event-log tests: replaying a recorded tournament reproduces it."""

import json

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.events import division_digest
from tournaments.models import Player, Tournament
from tournaments.replay import events_from_tournament, replay
from users.models import User


@tag("slow")
class ReplayTests(TestCase):
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

    def _build_logged_tournament(self):
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
                    for i, p in enumerate(self.players)
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
