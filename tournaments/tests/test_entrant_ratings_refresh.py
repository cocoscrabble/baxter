"""Re-pinning entrant ratings from the player table.

Entrants freeze their rating seed at registration so a roster pull cannot move a
running tournament (plans/PLAN_ENTRANTS.md decision 3). That stays true; this is
the director's deliberate override of it, one entrant at a time.

The three things worth pinning: manual ratings are never on offer, the event
records values rather than an intent to sync (a replay reads a player table that
has moved on), and the drift column never reaches the public list.
"""

import json
from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.commands import add_entrant, create_tournament
from tournaments.entrant_sync import rating_drift
from tournaments.models import Entrant, Player, RoundPairings
from users.models import User


class RefreshTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="td-refresh", password="pw")
        self.tournament = create_tournament(
            None, self.owner,
            {
                "name": "Champs", "location": "X", "start_date": "2026-05-01",
                "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
            },
        )
        self.division = self.tournament.divisions.get()
        self.player = Player.objects.create(
            name="Alec", player_number="0233", rating=1500, deviation=80.0,
            career_games=100, last_played=date(2026, 1, 1),
        )
        # Entered through the command, not Entrant.enter, so the log is
        # complete and the replay test has a tournament to rebuild.
        #
        # The seed is spelled out rather than left to the cascade because these
        # tests then move the player. `entrant_added` with no rating re-derives
        # the cascade at replay time, so a replay into this same database would
        # diverge at that event -- before reaching the one under test. Passing
        # rating_source keeps it CoCo-sourced rather than manual, which is what
        # a director confirming the offered rating produces.
        self.entrant = add_entrant(
            self.tournament, self.owner,
            {
                "division": "Open", "player": "0233", "number": 1,
                "rating": 1500, "rating_source": Entrant.COCO,
            },
        )

    def move_player(self, **fields):
        for key, value in fields.items():
            setattr(self.player, key, value)
        self.player.save()

    def url(self):
        return reverse(
            "division_refresh_ratings",
            args=[self.tournament.slug, self.division.slug],
        )

    def entrants_url(self):
        return reverse(
            "division_entrants", args=[self.tournament.slug, self.division.slug]
        )


class DriftDetectionTests(RefreshTestCase):
    def test_a_matching_entrant_is_not_drifted(self):
        self.assertEqual(rating_drift(self.division), [])

    def test_a_moved_rating_is_drifted(self):
        self.move_player(rating=1700)
        (drift,) = rating_drift(self.division)
        self.assertEqual((drift.old_rating, drift.new_rating), (1500, 1700))
        self.assertTrue(drift.rating_changed)

    def test_the_rest_of_the_seed_counts_as_drift_too(self):
        # Someone who played elsewhere gains games and a date without their
        # rating moving. The projection reads all four, so it is still stale.
        self.move_player(career_games=140, last_played=date(2026, 6, 1))
        (drift,) = rating_drift(self.division)
        self.assertFalse(drift.rating_changed)
        self.assertEqual(drift.seed["career_games"], 140)

    def test_a_manual_rating_is_never_offered(self):
        # Decision 3: a director who typed a rating was saying what this player
        # is worth. A sync does not get to overrule that.
        self.entrant.rating = 1234
        self.entrant.rating_source = Entrant.MANUAL
        self.entrant.save()
        self.move_player(rating=1700)
        self.assertEqual(rating_drift(self.division), [])

    def test_the_wespa_cascade_is_respected(self):
        # No CoCo rating: the seed falls to WESPA, and says so.
        self.move_player(rating=0, wespa_rating=1350)
        (drift,) = rating_drift(self.division)
        self.assertEqual(drift.seed["rating"], 1350)
        self.assertEqual(drift.seed["rating_source"], Entrant.WESPA)


class RefreshTests(RefreshTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_it_repins_the_whole_seed(self):
        self.move_player(
            rating=1700, deviation=42.0, career_games=140,
            last_played=date(2026, 6, 1),
        )
        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)

        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1700)
        self.assertEqual(self.entrant.deviation, 42.0)
        self.assertEqual(self.entrant.career_games, 140)
        self.assertEqual(self.entrant.last_played, date(2026, 6, 1))
        self.assertEqual(self.entrant.rating_source, Entrant.COCO)

    def test_only_the_ticked_entrants_move(self):
        other = Player.objects.create(
            name="Becky", player_number="0244", rating=1400
        )
        other_entrant = add_entrant(
            self.tournament, self.owner,
            {
                "division": "Open", "player": "0244", "number": 2,
                "rating": 1400, "rating_source": Entrant.COCO,
            },
        )
        self.move_player(rating=1700)
        other.rating = 1600
        other.save()

        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)

        self.entrant.refresh_from_db()
        other_entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1700)
        self.assertEqual(other_entrant.rating, 1400, "not ticked, not touched")

    def test_ticking_nothing_changes_nothing(self):
        self.move_player(rating=1700)
        response = self.client.post(self.url(), {}, follow=True)
        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1500)
        self.assertContains(response, "No entrants selected")

    def test_a_stale_tick_is_a_no_op_not_an_error(self):
        # The page was drawn, then the entrant was refreshed by someone else.
        response = self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)
        self.assertContains(response, "already match")
        self.assertFalse(
            self.tournament.events.filter(
                event_type="entrant_ratings_refreshed"
            ).exists()
        )

    def test_a_director_who_cannot_edit_is_refused(self):
        stranger = User.objects.create_user(username="stranger", password="pw")
        self.client.force_login(stranger)
        self.move_player(rating=1700)
        self.client.post(self.url(), {"entrants": ["0233"]})
        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.rating, 1500)


class EventTests(RefreshTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_it_is_logged(self):
        self.move_player(rating=1700)
        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)
        event = self.tournament.events.get(event_type="entrant_ratings_refreshed")
        self.assertEqual(event.payload["division"], "Open")
        (row,) = event.payload["entrants"]
        self.assertEqual(row["player"], "0233")
        self.assertEqual(row["rating"], 1700)

    def test_the_payload_carries_values_not_an_instruction_to_sync(self):
        # The whole point: a replay months later reads a player table that has
        # moved on. If the event meant "take whatever the roster says", the
        # replay would diverge -- and entrant ratings are in the digest.
        self.move_player(rating=1700)
        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)
        event = self.tournament.events.get(event_type="entrant_ratings_refreshed")

        self.move_player(rating=1900)  # the table moves again

        from tournaments.commands import refresh_entrant_ratings

        self.entrant.rating = 1500
        self.entrant.save()
        refresh_entrant_ratings(self.tournament, self.owner, event.payload)
        self.entrant.refresh_from_db()
        self.assertEqual(
            self.entrant.rating, 1700,
            "the recorded value, not what the player table says now",
        )

    def test_the_payload_is_json_serializable(self):
        # It is stored in a JSONField and read back by replay; a date would not
        # survive the round trip.
        self.move_player(rating=1700, last_played=date(2026, 6, 1))
        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)
        event = self.tournament.events.get(event_type="entrant_ratings_refreshed")
        self.assertEqual(
            json.loads(json.dumps(event.payload))["entrants"][0]["last_played"],
            "2026-06-01",
        )

    def test_a_replay_reproduces_the_division(self):
        # verify=True checks every event's recorded digest against the replayed
        # state, which is the assertion that matters here: entrant ratings are
        # in the digest, so a refresh that did not replay identically would
        # diverge right at this event.
        from tournaments.events import division_digest
        from tournaments.replay import events_from_tournament, replay

        self.move_player(rating=1700, career_games=140)
        self.client.post(self.url(), {"entrants": ["0233"]}, follow=True)
        self.division.refresh_from_db()
        recorded = division_digest(self.division)

        # Note: the player table is deliberately *not* moved before replaying.
        # `entrant_added` with no explicit rating re-derives the cascade at
        # replay time, so moving it would break that event, at seq 2, before
        # reaching this one. That the refresh itself carries its values rather
        # than re-reading them is pinned by the test above instead.
        ctx = replay(events_from_tournament(self.tournament), verify=True)
        replayed = ctx.tournament.divisions.get()
        self.assertEqual(division_digest(replayed), recorded)
        self.assertEqual(replayed.entrants.get().rating, 1700)


class AddEntrantSourceTests(RefreshTestCase):
    """A payload that spells out where the rating came from must not crash.

    ``rating_source`` is in _REGISTRATION_FIELDS *and* an argument of
    Entrant.enter, so forwarding it twice was a TypeError. Nothing in production
    sent one, which is why it went unnoticed -- but the field is advertised as a
    valid payload key, and replay restores snapshots with it.
    """

    def test_an_explicit_source_is_accepted(self):
        player = Player.objects.create(
            name="Cass", player_number="0255", rating=1600
        )
        entrant = add_entrant(
            self.tournament, self.owner,
            {
                "division": "Open", "player": player.player_number, "number": 3,
                "rating": 1600, "rating_source": Entrant.COCO,
            },
        )
        self.assertEqual(entrant.rating_source, Entrant.COCO)

    def test_a_recorded_none_stays_none(self):
        # The case Entrant.enter's rating_source argument exists for: (0, none)
        # must come back as none, not as the manual that a bare rating implies.
        player = Player.objects.create(
            name="Dev", player_number="0256", rating=0
        )
        entrant = add_entrant(
            self.tournament, self.owner,
            {
                "division": "Open", "player": player.player_number, "number": 4,
                "rating": 0, "rating_source": Entrant.NONE,
            },
        )
        self.assertEqual(entrant.rating_source, Entrant.NONE)


class DisplayTests(RefreshTestCase):
    def test_an_editor_sees_the_drift(self):
        self.client.force_login(self.owner)
        self.move_player(rating=1700)
        response = self.client.get(self.entrants_url())
        self.assertContains(response, "Player table")
        self.assertContains(response, "1700")
        self.assertContains(response, "Update ticked ratings")

    def test_the_public_list_shows_none_of_it(self):
        # The partial is shared with the embed. A stale seed is bookkeeping,
        # not a result.
        self.move_player(rating=1700)
        response = self.client.get(self.entrants_url())
        self.assertNotContains(response, "Player table")
        self.assertNotContains(response, "Update ticked ratings")
        self.assertContains(response, "1500", msg_prefix="the pinned rating stands")

    def test_the_embed_shows_none_of_it_either(self):
        self.move_player(rating=1700)
        response = self.client.get(
            reverse(
                "division_entrants_embed",
                args=[self.tournament.slug, self.division.slug],
            )
        )
        self.assertNotContains(response, "Player table")

    def test_nothing_is_shown_when_nothing_has_drifted(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.entrants_url())
        self.assertNotContains(response, "Player table")

    def test_a_started_division_is_warned_about(self):
        self.client.force_login(self.owner)
        self.move_player(rating=1700)
        RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.PUBLISHED
        )
        response = self.client.get(self.entrants_url())
        self.assertContains(response, "already under way")

    def test_a_draft_round_is_not_under_way(self):
        self.client.force_login(self.owner)
        self.move_player(rating=1700)
        RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.DRAFT
        )
        response = self.client.get(self.entrants_url())
        self.assertNotContains(response, "already under way")
