"""The registration page and its commands (plans/PLAN_ENTRANTS.md phase 3)."""

import json
from datetime import date

from django.test import TestCase, tag
from django.urls import reverse

from tournaments.models import (
    Division,
    DivisionSettings,
    Entrant,
    Player,
    Tournament,
)
from users.models import User


class RegistrationTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="td", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.tournament = Tournament.objects.create(
            name="Champs", location="Reno",
            start_date=date(2026, 4, 1), owner=self.owner,
        )
        self.tournament.editors.add(self.owner)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        DivisionSettings.objects.create(division=self.division)
        self.ann = Player.objects.create(
            name="Ann Lee", player_number="0001", rating=1600
        )
        self.bea = Player.objects.create(
            name="Bea Fox", player_number="0002", rating=0, wespa_rating=1400
        )
        self.unrated = Player.objects.create(
            name="Cy Ray", player_number="0003", rating=0
        )
        self.client.force_login(self.owner)

    def url(self, name="division_register"):
        return reverse(name, kwargs=self.division.slug_kwargs())

    def _post(self, **data):
        return self.client.post(self.url(), data, follow=True)

    def _guest_post(self, **data):
        """The guest form shares the registration fieldset with the add form, so
        its fields are prefixed — otherwise both render the same element ids."""
        return self._post(
            action="guest",
            **{f"guest-{k}": v for k, v in data.items()},
        )


class AddExistingTests(RegistrationTestCase):
    def test_adds_a_player_and_snapshots_their_coco_rating(self):
        response = self._post(
            action="add", player="0001", number=1, payment_note="",
        )
        self.assertEqual(response.status_code, 200)
        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player, self.ann)
        self.assertEqual((entrant.rating, entrant.rating_source), (1600, "coco"))

    def test_falls_back_to_wespa_when_there_is_no_coco_rating(self):
        self._post(action="add", player="0002", number=1, payment_note="")
        entrant = self.division.entrants.get()
        self.assertEqual((entrant.rating, entrant.rating_source), (1400, "wespa"))

    def test_an_unrated_player_pins_nothing(self):
        self._post(action="add", player="0003", number=1, payment_note="")
        entrant = self.division.entrants.get()
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

    def test_an_explicit_rating_overrides_the_cascade_and_is_manual(self):
        self._post(
            action="add", player="0001", number=1, rating=1234, payment_note="",
        )
        entrant = self.division.entrants.get()
        self.assertEqual((entrant.rating, entrant.rating_source), (1234, "manual"))

    def test_registration_flags_are_recorded(self):
        self._post(
            action="add", player="0001", number=1, tentative="on",
            playing_up="on", payment_note="cheque in the post",
        )
        entrant = self.division.entrants.get()
        self.assertTrue(entrant.tentative)
        self.assertTrue(entrant.playing_up)
        self.assertFalse(entrant.paid)
        self.assertEqual(entrant.payment_note, "cheque in the post")

    def test_entering_the_same_player_twice_is_refused(self):
        self._post(action="add", player="0001", number=1, payment_note="")
        response = self._post(action="add", player="0001", number=2, payment_note="")
        self.assertEqual(self.division.entrants.count(), 1)
        self.assertContains(response, "already entered")

    def test_a_missing_player_is_reported_not_crashed(self):
        response = self._post(action="add", player="", number=1, payment_note="")
        self.assertEqual(self.division.entrants.count(), 0)
        self.assertContains(response, "Pick a player first")


class PaidClearsTentativeTests(RegistrationTestCase):
    """Marking someone paid confirms them by default, and the organizer can
    override that in either direction (decision 5)."""

    def test_paid_alone_leaves_them_confirmed(self):
        self._post(action="add", player="0001", number=1, paid="on", payment_note="")
        entrant = self.division.entrants.get()
        self.assertTrue(entrant.paid)
        self.assertFalse(entrant.tentative)

    def test_paid_and_tentative_together_is_an_override_that_sticks(self):
        self._post(
            action="add", player="0001", number=1, paid="on", tentative="on",
            payment_note="",
        )
        entrant = self.division.entrants.get()
        self.assertTrue(entrant.paid)
        self.assertTrue(
            entrant.tentative,
            "an organizer who re-ticks tentative on a paid entrant means it",
        )


class GuestTests(RegistrationTestCase):
    def test_creates_a_provisional_player_and_enters_them(self):
        self._guest_post(
            name="Gwen Guest", wespa_rating=1450, number=1, payment_note="",
        )
        player = Player.objects.get(name="Gwen Guest")
        self.assertTrue(player.is_provisional)
        self.assertTrue(player.player_number.startswith("T-"))
        self.assertEqual(player.rating, 0, "a guest has no CoCo rating")
        self.assertEqual(player.wespa_rating, 1450)

        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player, player)
        self.assertEqual((entrant.rating, entrant.rating_source), (1450, "wespa"))

    def test_a_guest_with_no_rating_at_all(self):
        self._guest_post(name="Nobody", number=1, payment_note="")
        entrant = self.division.entrants.get()
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

    def test_a_guest_may_share_a_name_with_a_member(self):
        """Their T- number is a first-class identity, so this is ordinary."""
        self._guest_post(name="Ann Lee", number=1, payment_note="")
        self.assertEqual(Player.objects.filter(name="Ann Lee").count(), 2)
        numbers = set(
            Player.objects.filter(name="Ann Lee").values_list(
                "player_number", flat=True
            )
        )
        self.assertEqual(len(numbers), 2)


class UpdateEntrantTests(RegistrationTestCase):
    def setUp(self):
        super().setUp()
        self.entrant = Entrant.enter(self.division, self.ann, 1)

    def test_confirming_and_marking_paid(self):
        self.entrant.tentative = True
        self.entrant.save(update_fields=["tentative"])
        self._post(
            action="update", entrant=self.entrant.pk, number=1, paid="on",
            rating=self.entrant.rating, payment_note="cash",
        )
        self.entrant.refresh_from_db()
        self.assertTrue(self.entrant.paid)
        self.assertFalse(self.entrant.tentative)
        self.assertEqual(self.entrant.payment_note, "cash")

    def test_saving_without_changing_the_rating_leaves_its_source_alone(self):
        """The form prefills the rating, so a plain re-save must not silently
        convert a cascaded snapshot into a hand-set one."""
        self._post(
            action="update", entrant=self.entrant.pk, number=1,
            rating=self.entrant.rating, paid="on", payment_note="",
        )
        self.entrant.refresh_from_db()
        self.assertEqual(
            (self.entrant.rating, self.entrant.rating_source), (1600, "coco")
        )
        self.assertTrue(self.entrant.paid)

    def test_fixing_a_rating_makes_it_manual(self):
        self._post(
            action="update", entrant=self.entrant.pk, number=1, rating=1750,
            payment_note="",
        )
        self.entrant.refresh_from_db()
        self.assertEqual(
            (self.entrant.rating, self.entrant.rating_source), (1750, "manual")
        )

    def test_the_edit_form_is_prefilled(self):
        self.entrant.tentative = True
        self.entrant.payment_note = "owes 20"
        self.entrant.save(update_fields=["tentative", "payment_note"])
        response = self.client.get(f"{self.url()}?entrant={self.entrant.pk}")
        self.assertEqual(response.status_code, 200)
        form = response.context["registration_form"]
        self.assertEqual(form.initial["rating"], 1600)
        self.assertTrue(form.initial["tentative"])
        self.assertEqual(form.initial["payment_note"], "owes 20")


class SearchTests(RegistrationTestCase):
    def test_search_finds_players_by_name(self):
        response = self.client.get(self.url(), {"q": "ann"})
        numbers = [r.player_number for r in response.context["search_results"]]
        self.assertEqual(numbers, ["0001"])

    def test_search_excludes_players_already_entered(self):
        Entrant.enter(self.division, self.ann, 1)
        response = self.client.get(self.url(), {"q": "ann"})
        self.assertEqual(response.context["search_results"], [])

    def test_an_empty_query_returns_nothing_rather_than_the_whole_roster(self):
        response = self.client.get(self.url(), {"q": ""})
        self.assertEqual(response.context["search_results"], [])

    def test_the_search_fragment_endpoint_renders_rows(self):
        response = self.client.get(
            reverse(
                "division_register_search", kwargs=self.division.slug_kwargs()
            ),
            {"q": "bea"},
        )
        self.assertContains(response, "Bea Fox")
        self.assertContains(response, "0002")


class PermissionTests(RegistrationTestCase):
    def test_a_non_editor_cannot_open_the_page(self):
        self.client.force_login(self.other)
        self.assertNotEqual(self.client.get(self.url()).status_code, 200)

    def test_a_non_editor_cannot_register_anyone(self):
        self.client.force_login(self.other)
        self.client.post(
            self.url(), {"action": "add", "player": "0001", "number": 1}
        )
        self.assertEqual(self.division.entrants.count(), 0)


@tag("slow")
class RegistrationEventTests(RegistrationTestCase):
    """Every registration action is in the log, keyed on the player number."""

    def setUp(self):
        super().setUp()
        # A tournament built through the command, so the log is complete enough
        # to replay: the base fixture creates one directly, which is fine for
        # the view tests but leaves no tournament_created event.
        from tournaments.commands import create_tournament

        self.tournament = create_tournament(
            None, self.owner,
            {
                "name": "Logged", "location": "Reno",
                "start_date": "2026-04-01", "editors": [],
                "default_division": {"name": "Open", "pairing_seed": 7},
            },
        )
        self.division = self.tournament.divisions.get()

    def _types(self):
        return [e.event_type for e in self.tournament.events.order_by("seq")]

    def test_adding_logs_entrant_added_keyed_on_the_number(self):
        self._post(action="add", player="0001", number=1, payment_note="")
        event = self.tournament.events.get(event_type="entrant_added")
        self.assertEqual(event.payload["player"], "0001")
        self.assertEqual(event.payload["division"], "Open")
        self.assertNotIn("Ann Lee", json.dumps(event.payload))

    def test_creating_a_guest_logs_the_player_then_the_entrant(self):
        self._guest_post(name="Gwen", number=1, payment_note="")
        self.assertEqual(self._types()[-2:], ["player_created", "entrant_added"])
        created = self.tournament.events.get(event_type="player_created")
        self.assertEqual(created.payload["name"], "Gwen")
        self.assertTrue(created.payload["player_number"].startswith("T-"))

    def test_updating_logs_entrant_updated(self):
        entrant = Entrant.enter(self.division, self.ann, 1)
        self._post(
            action="update", entrant=entrant.pk, number=1, paid="on",
            rating=1600, payment_note="",
        )
        event = self.tournament.events.get(event_type="entrant_updated")
        self.assertEqual(event.payload["player"], "0001")
        self.assertTrue(event.payload["paid"])

    def test_the_whole_registration_replays(self):
        from tournaments.events import division_digest
        from tournaments.replay import events_from_tournament, replay

        self._post(action="add", player="0001", number=1, payment_note="")
        self._guest_post(
            name="Gwen", wespa_rating=1450, number=2,
            tentative="on", payment_note="pending",
        )
        entrant = self.division.entrants.get(player=self.ann)
        self._post(
            action="update", entrant=entrant.pk, number=1, rating=1720,
            paid="on", payment_note="cash",
        )
        recorded = division_digest(self.division)

        ctx = replay(events_from_tournament(self.tournament), verify=True)
        self.assertEqual(
            division_digest(ctx.tournament.divisions.get()), recorded
        )
