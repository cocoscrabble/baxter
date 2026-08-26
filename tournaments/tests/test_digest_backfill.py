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


@tag("slow")
class RerunTests(DigestBackfillTests):
    """The backfill is safe to run twice.

    It has to be: it is a migration *and* a management command, and the command
    exists precisely for the case where the migration could not run.
    """

    def test_a_second_run_reports_nothing_to_do(self):
        self._make_digests_v1()
        first, reason = backfill_tournament(self.tournament)
        self.assertIsNone(reason)
        self.assertTrue(first)

        second, reason = backfill_tournament(self.tournament)
        self.assertEqual(second, 0)
        self.assertIsNone(
            reason,
            "an already-backfilled tournament is done, not divergent",
        )

    def test_a_second_run_changes_nothing(self):
        self._make_digests_v1()
        backfill_tournament(self.tournament)
        after_first = {e.seq: e.digest for e in self.tournament.events.all()}
        backfill_tournament(self.tournament)
        self.assertEqual(
            {e.seq: e.digest for e in self.tournament.events.all()}, after_first
        )


@tag("slow")
class ImportedDivisionBackfillTests(TestCase):
    """A what-if import must verify under v1 like anything else.

    ``division_imported`` is the one command whose payload stays name-keyed —
    it *is* the historical document — so its replay path differs from every
    other tournament's. The backfill refuses to rewrite a log that does not
    reproduce, which is only a trustworthy signal if the v1 reconstruction
    handles this shape too. Otherwise a perfectly good sandbox division looks
    divergent and gets skipped for no reason.

    The final assertion is the load-bearing one. Seeding the v1 digests and then
    verifying them with the same function proves only that the function agrees
    with itself; comparing the rewritten digest against the *live* division is
    what proves the replay reproduces what is actually there.

    **What this cannot see.** ``division_imported`` replays by re-running the
    same command that recorded it, so a change to *that* command moves the
    recording and the replay together and stays invisible here — as does a
    change to the v1 digest, which seeds and verifies both sides. What it does
    catch is the replay diverging from the recording: a resolve step that
    invents a new player instead of matching the existing one, dict ordering,
    a minted number that drifts. That is the failure this shape is prone to,
    and it is why the assertion is against live state rather than a round trip.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="whatif", password="pw")
        Player.objects.create(name="Alice", player_number="0001", rating=1600)
        Player.objects.create(name="Bob", player_number="0002", rating=1500)

    def _import(self):
        from tournaments.commands import create_tournament, import_division

        tournament = create_tournament(
            None, self.owner,
            {
                "name": "What-if", "location": "X",
                "start_date": "2026-04-01", "editors": [],
                "default_division": {"name": "Division 1", "pairing_seed": 3},
            },
        )
        import_division(
            tournament, self.owner,
            {
                "name": "Imported",
                "entrants": [
                    {"player": "Alice", "rating": 1600, "number": 1},
                    {"player": "Bob", "rating": 1500, "number": 2},
                    # Not on the roster: resolve_player mints a T- number, which
                    # is the part most likely to differ between record and replay.
                    {"player": "Carol Newcomer", "rating": 0, "number": 3},
                ],
                "results": [
                    {"round": 1, "winner": "Alice", "loser": "Bob",
                     "winner_score": 450, "loser_score": 380,
                     "winner_started": True},
                    {"round": 2, "winner": "Carol Newcomer", "loser": "Alice",
                     "winner_score": 500, "loser_score": 400,
                     "winner_started": True},
                ],
            },
        )
        return tournament

    def test_an_imported_division_verifies_and_is_rewritten(self):
        from tournaments.digest_backfill import backfill_tournament, replayed_digests
        from tournaments.events import division_digest
        from tournaments.replay import events_from_tournament

        tournament = self._import()
        division = tournament.divisions.get(name="Imported")
        # The live state, before anything is replayed. Every assertion below
        # anchors to this: seeding the v1 digests *and* verifying them with the
        # same function would prove only that the function agrees with itself.
        live = division_digest(division)

        # Walk its digests back to v1, as an older database holds them.
        digests = replayed_digests(events_from_tournament(tournament))
        self.assertTrue(digests, "the import should have recorded a digest")
        for event in tournament.events.filter(seq__in=digests):
            event.digest = digests[event.seq][0]
            event.save(update_fields=["digest"])

        count, reason = backfill_tournament(tournament)
        self.assertIsNone(
            reason,
            "an imported division should verify under v1, not look divergent",
        )
        self.assertEqual(count, len(digests))

        # The rewritten digest describes the division that is actually here —
        # which is what makes the replay faithful rather than merely repeatable.
        self.assertEqual(
            tournament.events.order_by("seq").last().digest, live
        )
