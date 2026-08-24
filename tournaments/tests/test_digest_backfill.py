"""The v1 -> v2 digest backfill (plans/PLAN_PLAYER_IDENTITY.md phase 3d).

The backfill rewrites an append-only log, so what is tested here is mostly the
restraint: it must refuse to touch a tournament whose log does not already
reproduce its recorded digests, because such a log was divergent before the
backfill arrived and overwriting it would destroy the evidence.
"""

import json

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.digest_backfill import backfill_all, backfill_tournament
from tournaments.events import division_digest
from tournaments.models import Player, Tournament
from users.models import User


@tag("slow")
class DigestBackfillTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.players = [
            Player.objects.create(name=n, player_number=f"000{i}", rating=r)
            for i, (n, r) in enumerate(
                [("Alice", 1600), ("Bob", 1500), ("Cara", 1400), ("Dan", 1300)], 1
            )
        ]
        self.client.force_login(self.owner)
        self.tournament, self.division = self._build()

    def _post_json(self, name, division, body):
        return self.client.post(
            reverse(name, kwargs=division.slug_kwargs()),
            json.dumps(body),
            content_type="application/json",
        )

    def _build(self):
        self.client.post(
            reverse("tournament_create"),
            {"name": "Champs", "location": "Reno", "start_date": "2026-03-15",
             "editor_usernames": ""},
        )
        tournament = Tournament.objects.get(name="Champs")
        division = tournament.divisions.get()
        self._post_json(
            "division_entrants_edit", division,
            {"rows": [
                {"number": i + 1, "player": p.pk, "dropped": False}
                for i, p in enumerate(self.players)
            ]},
        )
        self._post_json(
            "division_round_pairings", division,
            {"blocks": [{"pairing": "KotH", "rounds": 2, "pair_from": 1}]},
        )
        self.client.get(
            reverse("division_pair_rounds", kwargs=division.slug_kwargs())
        )
        self.client.post(
            reverse("publish_round", kwargs=division.slug_kwargs()), {"round": 1}
        )
        return tournament, division

    def _make_digests_v1(self):
        """Rewrite the log's digests to the v1 form, as an old database holds them."""
        from tournaments.digest_backfill import replayed_digests
        from tournaments.replay import events_from_tournament

        digests = replayed_digests(events_from_tournament(self.tournament))
        for event in self.tournament.events.filter(seq__in=digests):
            event.digest = digests[event.seq][0]
            event.save(update_fields=["digest"])
        return digests

    def test_a_clean_tournament_is_rewritten_to_v2(self):
        digests = self._make_digests_v1()
        stored = {e.seq: e.digest for e in self.tournament.events.all() if e.digest}
        self.assertTrue(stored)
        # Precondition: the log is in the old vocabulary and does not verify.
        self.assertNotEqual(
            stored[max(stored)], digests[max(digests)][1]
        )

        count, reason = backfill_tournament(self.tournament)

        self.assertIsNone(reason)
        self.assertEqual(count, len(stored))
        for event in self.tournament.events.filter(seq__in=digests):
            self.assertEqual(event.digest, digests[event.seq][1])
        # And the log now verifies against the live state.
        self.assertEqual(
            self.tournament.events.order_by("seq").last().digest,
            division_digest(self.division),
        )

    def test_a_divergent_tournament_is_skipped_and_reported(self):
        self._make_digests_v1()
        # A log that no longer describes its own state: one digest is wrong.
        target = self.tournament.events.order_by("seq").last()
        original = target.digest
        target.digest = "0" * 64
        target.save(update_fields=["digest"])

        count, reason = backfill_tournament(self.tournament)

        self.assertEqual(count, 0)
        self.assertIn("do not reproduce", reason)
        self.assertIn(f"seq {target.seq}", reason)
        # Nothing was touched — not even the digests that *were* fine.
        target.refresh_from_db()
        self.assertEqual(target.digest, "0" * 64)
        self.assertNotEqual(target.digest, original)
        others = self.tournament.events.exclude(seq=target.seq).exclude(digest="")
        self.assertTrue(others.exists())

    def test_the_replay_it_runs_leaves_nothing_behind(self):
        before = set(Tournament.objects.values_list("pk", flat=True))
        self._make_digests_v1()
        backfill_tournament(self.tournament)
        self.assertEqual(
            set(Tournament.objects.values_list("pk", flat=True)), before
        )

    def test_backfill_all_reports_each_tournament(self):
        self._make_digests_v1()
        lines = []
        done, skipped = backfill_all(log=lines.append)
        self.assertEqual((done, skipped), (1, 0))
        self.assertEqual(len(lines), 1)
        self.assertIn("Champs", lines[0])

    def test_the_schema_guard_passes_on_a_migrated_database(self):
        from tournaments.digest_backfill import schema_mismatch

        self.assertIsNone(schema_mismatch())

    def test_the_schema_guard_names_a_missing_column(self):
        """What the migration checks before it replays anything.

        An earlier draft of the backfill ran as migration 0038, and adding the
        entrant fields after it made every replay die on a missing column —
        silently leaving every digest at v1. The guard turns that into a
        refusal that says what is wrong.
        """
        from django.db import connection

        from tournaments.digest_backfill import schema_mismatch

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE tournaments_entrant DROP COLUMN payment_note"
            )
        try:
            self.assertEqual(
                schema_mismatch(), "tournaments_entrant.payment_note does not exist yet"
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE tournaments_entrant "
                    "ADD COLUMN payment_note text NOT NULL DEFAULT ''"
                )

    def test_a_tournament_with_no_digests_is_left_alone(self):
        self.tournament.events.update(digest="")
        self.assertEqual(backfill_tournament(self.tournament), (0, None))
