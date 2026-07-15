"""Phase 1 event-log tests: the state digest and the recorder."""

from django.test import TestCase
from django.urls import reverse

from tournaments.events import division_digest, division_state, record_event
from tournaments.models import (
    Division,
    Entrant,
    Pairing,
    Player,
    ResultSlip,
    RoundPairings,
)
from tournaments.tests.test_models import setUpTournament


class DivisionDigestTests(TestCase):
    def setUp(self):
        setUpTournament(self)
        self.division.entrants.all().delete()
        self.filler = Player.objects.create(
            name="Filler", player_number="900", rating=1000
        )

    def _build_state(self, division):
        """A small finished round: two entrants, one pairing, one result."""
        e1 = Entrant.objects.create(
            division=division, player=self.player1, number=1
        )
        e2 = Entrant.objects.create(
            division=division, player=self.player2, number=2
        )
        rp = RoundPairings.objects.create(
            division=division, round=1, status=RoundPairings.FINISHED
        )
        pairing = Pairing.objects.create(
            division=division, round=1, round_pairings=rp,
            first=e1, second=e2, table=1,
        )
        ResultSlip.objects.create(
            division=division, round=1, pairing=pairing,
            winner=e1, winner_score=450, loser=e2, loser_score=380,
            winner_started=True,
        )

    def _churn_pks(self):
        # Create and delete rows so the auto-increment sequences advance, giving
        # the next-built division higher pks for the same logical state.
        for i in range(5):
            junk = Division.objects.create(
                name=f"junk{i}", tournament=self.tournament
            )
            Entrant.objects.create(division=junk, player=self.filler, number=1)
            junk.delete()

    def test_digest_is_stable_across_pk_renumbering(self):
        d1 = Division.objects.create(name="A", tournament=self.tournament)
        self._build_state(d1)
        digest1 = division_digest(d1)

        self._churn_pks()

        d2 = Division.objects.create(name="B", tournament=self.tournament)
        self._build_state(d2)
        digest2 = division_digest(d2)

        # Same logical state, different pks (and even different division name,
        # which the digest excludes) -> identical digest.
        self.assertEqual(digest1, digest2)
        self.assertNotEqual(d1.entrants.first().pk, d2.entrants.first().pk)

    def test_digest_changes_with_state(self):
        d1 = Division.objects.create(name="A", tournament=self.tournament)
        self._build_state(d1)
        before = division_digest(d1)
        # Withdraw an entrant -> state (and digest) changes.
        e = d1.entrants.get(player=self.player1)
        e.dropped = True
        e.save(update_fields=["dropped"])
        self.assertNotEqual(before, division_digest(d1))

    def test_state_is_pk_free_and_name_keyed(self):
        d1 = Division.objects.create(name="A", tournament=self.tournament)
        self._build_state(d1)
        state = division_state(d1)
        self.assertEqual(
            state["entrants"], [[1, "Alice", False], [2, "Bob", False]]
        )
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(state["results"][0][1], "Alice")  # winner by name


class RecordEventTests(TestCase):
    def setUp(self):
        setUpTournament(self)

    def test_seq_increments_per_tournament(self):
        e1 = record_event(self.tournament, "division_created", {"name": "X"})
        e2 = record_event(self.tournament, "division_created", {"name": "Y"})
        self.assertEqual([e1.seq, e2.seq], [1, 2])
        self.assertEqual(self.tournament.events.count(), 2)

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(ValueError):
            record_event(self.tournament, "not_a_real_type", {})
        self.assertEqual(self.tournament.events.count(), 0)

    def test_metadata_stored(self):
        e = record_event(
            self.tournament,
            "result_added",
            {"round": 1},
            actor=self.owner,
            actor_session="hashed",
            division=self.division,
            digest="deadbeef",
        )
        self.assertEqual(e.actor, self.owner)
        self.assertEqual(e.actor_session, "hashed")
        self.assertEqual(e.division, self.division)
        self.assertEqual(e.digest, "deadbeef")


class CrudCommandTests(TestCase):
    """Phase 2 (CRUD cluster): the tournament/division lifecycle commands log."""

    def setUp(self):
        setUpTournament(self)

    def test_create_tournament_logs_with_default_division_seed(self):
        from tournaments.commands import create_tournament

        t = create_tournament(
            None,
            self.owner,
            {
                "name": "Nationals",
                "location": "Reno",
                "start_date": "2026-08-01",
                "editors": [],
            },
        )
        event = t.events.get()
        self.assertEqual(event.event_type, "tournament_created")
        self.assertEqual(event.payload["name"], "Nationals")
        self.assertEqual(event.actor, self.owner)
        # The default division and its seed are recorded so replay reproduces
        # the same random-strategy pairings.
        self.assertEqual(t.divisions.count(), 1)
        self.assertIn("pairing_seed", event.payload["default_division"])
        div = t.divisions.get()
        self.assertEqual(
            div.settings.pairing_seed, event.payload["default_division"]["pairing_seed"]
        )

    def test_create_division_records_seed(self):
        from tournaments.commands import create_division

        div = create_division(self.tournament, self.owner, {"name": "Novice"})
        event = self.tournament.events.get()
        self.assertEqual(event.event_type, "division_created")
        self.assertEqual(event.division, div)
        self.assertEqual(event.payload["pairing_seed"], div.settings.pairing_seed)

    def test_rename_and_delete_log_events(self):
        from tournaments.commands import delete_division, rename_division

        rename_division(
            self.tournament, self.owner, {"old_name": "Open", "new_name": "Championship"}
        )
        delete_division(self.tournament, self.owner, {"name": "Championship"})
        types = list(
            self.tournament.events.order_by("seq").values_list("event_type", flat=True)
        )
        self.assertEqual(types, ["division_renamed", "division_deleted"])

    def test_noop_publish_records_no_event(self):
        # A command that validates to a no-op (nothing to publish) records
        # nothing, via EventResult(record=False).
        from tournaments.commands import publish_all_rounds

        publish_all_rounds(self.tournament, self.owner, {"division": "Open"})
        self.assertEqual(self.tournament.events.count(), 0)


class WriteGuardTests(TestCase):
    """The opt-in strict write guard catches mutations that skip a command."""

    def setUp(self):
        setUpTournament(self)

    def test_bare_write_raises_under_strict_guard(self):
        from tournaments.events import strict_write_guard

        with self.assertRaises(RuntimeError):
            with strict_write_guard():
                Division.objects.create(name="Loose", tournament=self.tournament)

    def test_command_write_allowed_under_strict_guard(self):
        from tournaments.commands import create_division
        from tournaments.events import strict_write_guard

        with strict_write_guard():
            create_division(self.tournament, self.owner, {"name": "Novice"})
        self.assertTrue(self.tournament.divisions.filter(name="Novice").exists())

    def test_derived_write_allowed_under_strict_guard(self):
        from tournaments.events import derived_writes, strict_write_guard

        newcomer = Player.objects.create(name="Zed", player_number="777", rating=1200)
        with strict_write_guard(), derived_writes():
            Entrant.objects.create(
                division=self.division, player=newcomer, number=99
            )
        self.assertTrue(self.division.entrants.filter(number=99).exists())


class GridEventTests(TestCase):
    """A grid save records an event with a pk-free (name-based) payload."""

    def setUp(self):
        setUpTournament(self)
        self.division.entrants.all().delete()

    def test_entrants_grid_save_logs_portable_rows(self):
        import json

        self.client.login(username="owner", password="testpass123")
        url = reverse("division_entrants_edit", kwargs=self.division.slug_kwargs())
        payload = {
            "rows": [
                {"number": 1, "player": self.player1.pk, "dropped": False},
                {"number": 2, "player": self.player2.pk, "dropped": False},
            ]
        }
        response = self.client.post(
            url, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        event = self.tournament.events.get()
        self.assertEqual(event.event_type, "entrants_saved")
        self.assertEqual(event.division, self.division)
        self.assertEqual(event.actor, self.owner)
        # Players are recorded by name, not pk.
        players = {row["player"] for row in event.payload["rows"]}
        self.assertEqual(players, {"Alice", "Bob"})
