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
        """The guest branch of the unified add form.

        One form serves all three ways in, so there is no prefix any more; the
        branch is told apart by the button that submitted it.
        """
        return self._post(action="add", guest="1", **data)


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
        self._guest_post(name="Gwen Guest", rating=1450, payment_note="")
        player = Player.objects.get(name="Gwen Guest")
        self.assertTrue(player.is_provisional)
        self.assertTrue(player.player_number.startswith("T-"))
        self.assertEqual(player.rating, 0, "a guest has no CoCo rating")
        self.assertIsNone(
            player.wespa_rating,
            "a typed rating is the director's judgement, not a WESPA number",
        )

        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player, player)
        self.assertEqual((entrant.rating, entrant.rating_source), (1450, "manual"))

    def test_a_guest_with_no_rating_at_all(self):
        self._guest_post(name="Nobody", payment_note="")
        entrant = self.division.entrants.get()
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))

    def test_a_guest_may_share_a_name_with_a_member(self):
        """Their T- number is a first-class identity, so this is allowed —
        it just has to be said out loud, since a repeated name is usually a typo
        (see DuplicateGuestNameTests). ``confirm`` is the director saying it."""
        self._post(action="add", guest="confirm", name="Ann Lee", payment_note="")
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
        self._guest_post(name="Gwen", payment_note="")
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

        self._post(action="add", player="0001", payment_note="")
        self._guest_post(
            name="Gwen", rating=1450, tentative="on", payment_note="pending",
        )
        entrant = self.division.entrants.get(player=self.ann)
        self._post(
            action="update", entrant=entrant.pk, rating=1720,
            paid="on", payment_note="cash",
        )
        recorded = division_digest(self.division)

        ctx = replay(events_from_tournament(self.tournament), verify=True)
        self.assertEqual(
            division_digest(ctx.tournament.divisions.get()), recorded
        )


class EntrantDisplayTests(RegistrationTestCase):
    """The public entrant list's display conventions (phase 4a).

    A rating from WESPA is italic, a tentative entrant is marked ``*`` and one
    playing up ``^`` — and none of that may be the *only* way to tell, so each
    marked row carries a textual equivalent too (#42).
    """

    def _url(self):
        return reverse("division_entrants", kwargs=self.division.slug_kwargs())

    def test_a_plain_entrant_carries_no_markers_and_no_legend(self):
        Entrant.enter(self.division, self.ann, 1)
        response = self.client.get(self._url())
        self.assertContains(response, "Ann Lee")
        self.assertNotContains(response, "entrant-legend")
        self.assertNotContains(response, "rating-wespa")

    def test_a_wespa_rating_is_italic_and_says_so(self):
        Entrant.enter(self.division, self.bea, 1)
        response = self.client.get(self._url())
        self.assertContains(response, "rating-wespa")
        self.assertContains(response, "(WESPA rating)")
        self.assertContains(response, "WESPA rating</dd>")

    def test_an_unrated_entrant_shows_zero(self):
        Entrant.enter(self.division, self.unrated, 1)
        response = self.client.get(self._url())
        self.assertContains(response, "Cy Ray")
        self.assertNotContains(response, "rating-wespa")

    def test_tentative_is_marked_and_also_spelled_out(self):
        Entrant.enter(self.division, self.ann, 1, tentative=True)
        response = self.client.get(self._url())
        body = response.content.decode()
        self.assertIn("Ann Lee*", body)
        self.assertIn("(tentative)", body)
        self.assertIn("Tentative</dd>", body)

    def test_playing_up_is_marked_and_also_spelled_out(self):
        Entrant.enter(self.division, self.ann, 1, playing_up=True)
        body = self.client.get(self._url()).content.decode()
        self.assertIn("Ann Lee^", body)
        self.assertIn("(playing up)", body)
        self.assertIn("Playing up</dd>", body)

    def test_the_legend_only_explains_markers_that_are_present(self):
        Entrant.enter(self.division, self.ann, 1, tentative=True)
        body = self.client.get(self._url()).content.decode()
        self.assertIn("Tentative</dd>", body)
        self.assertNotIn("Playing up</dd>", body)

    def test_payment_is_shown_to_an_editor(self):
        Entrant.enter(self.division, self.ann, 1, paid=True, payment_note="cash")
        response = self.client.get(self._url())
        self.assertContains(response, "cash")

    def test_payment_is_absent_for_everyone_else(self):
        Entrant.enter(self.division, self.ann, 1, paid=True, payment_note="cash")
        self.client.logout()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "cash")
        self.assertNotContains(response, "Unpaid")


class EntrantsEmbedTests(RegistrationTestCase):
    """The chrome-free fragment the CoCo site iframes (phase 4b)."""

    def _url(self):
        return reverse(
            "division_entrants_embed", kwargs=self.division.slug_kwargs()
        )

    def test_it_renders_the_same_conventions_without_chrome(self):
        Entrant.enter(self.division, self.bea, 1, tentative=True)
        self.client.logout()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Bea Fox*", body)
        self.assertIn("rating-wespa", body)
        # No site chrome: no nav, no page title bar.
        self.assertNotIn("<nav", body)
        self.assertNotIn("COCO logo", body)

    def test_it_may_be_framed(self):
        """Without this the embedding page renders a blank box and a console
        error, which is a miserable thing to debug from the far end."""
        response = self.client.get(self._url())
        self.assertNotIn("X-Frame-Options", response.headers)

    def test_payment_is_absent_even_for_an_editor(self):
        """An embedded page has no business varying by viewer."""
        Entrant.enter(self.division, self.ann, 1, paid=True, payment_note="cash")
        response = self.client.get(self._url())  # still logged in as the owner
        self.assertNotContains(response, "cash")
        self.assertNotContains(response, "Unpaid")
        # Explicitly False, not merely absent. Nothing sets can_edit on this view
        # today, so leaving it unset would pass this test by accident and break
        # the moment someone adds a nav mixin or a context processor.
        self.assertIs(response.context["can_edit"], False)

    def test_a_test_division_is_hidden_from_a_signed_out_visitor(self):
        self.division.is_test = True
        self.division.save(update_fields=["is_test"])
        self.client.logout()
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_a_test_division_is_hidden_from_its_own_editor_too(self):
        """An embed contains strictly what a signed-out visitor would get.

        The iframe is loaded by the *visitor's* browser with the visitor's
        cookies, so a director browsing the CoCo site while signed into Baxter
        must be served the same bytes as everyone else. The ordinary division
        page still shows them their test division; this one must not.
        """
        self.division.is_test = True
        self.division.save(update_fields=["is_test"])
        # Still logged in as the owner, who *can* edit this tournament.
        self.assertTrue(self.tournament.can_edit(self.owner))
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        # …and the ordinary page still works for them, so this is the embed's
        # rule rather than a change to what an editor may see.
        ordinary = reverse(
            "division_entrants", kwargs=self.division.slug_kwargs()
        )
        self.assertEqual(self.client.get(ordinary).status_code, 200)

    def test_the_bytes_do_not_depend_on_who_is_asking(self):
        """The whole rule, in one assertion."""
        Entrant.enter(
            self.division, self.ann, 1, paid=True, payment_note="cash",
            tentative=True,
        )
        Entrant.enter(self.division, self.bea, 2, playing_up=True)

        as_editor = self.client.get(self._url()).content
        self.client.logout()
        as_visitor = self.client.get(self._url()).content
        self.assertEqual(as_editor, as_visitor)

    def test_an_empty_division_says_so(self):
        response = self.client.get(self._url())
        self.assertContains(response, "No entrants yet")


class PlayerSourceSeamTests(RegistrationTestCase):
    """The registration page goes through the seam, not the Player table.

    That is the whole point of decision 11: swapping in a registry-backed source
    later must change no view code. A fake source proves the page never reaches
    around it.
    """

    def _fake_source(self, records):
        from tournaments.player_source import PlayerSource

        class FakeSource(PlayerSource):
            def __init__(self):
                self.searched = []

            def search(self, query):
                self.searched.append(query)
                return list(records)

            def fetch(self, player_number):
                return next(
                    (r for r in records if r.player_number == player_number), None
                )

            def mint_number(self, name):
                return "X-1"

        return FakeSource()

    def test_the_search_comes_from_the_source(self):
        from unittest.mock import patch

        from tournaments.player_source import PlayerRecord

        fake = self._fake_source([
            PlayerRecord(player_number="9900", name="Remote Rita", rating=1710)
        ])
        with patch("tournaments.views.get_player_source", return_value=fake):
            response = self.client.get(self.url(), {"q": "rita"})

        self.assertEqual(fake.searched, ["rita"])
        self.assertContains(response, "Remote Rita")
        # The local roster is not consulted, so a local-only player is absent.
        self.assertNotContains(response, "Ann Lee")

    def test_entering_a_player_the_source_returned(self):
        from unittest.mock import patch

        from tournaments.player_source import PlayerRecord

        # The player has to exist locally to be entered; the source decides
        # *which* one, the database still holds them.
        fake = self._fake_source([
            PlayerRecord(player_number="0001", name="Ann Lee", rating=1600)
        ])
        with patch("tournaments.views.get_player_source", return_value=fake):
            self._post(action="add", player="0001", number=1, payment_note="")

        self.assertEqual(self.division.entrants.get().player, self.ann)

    def test_the_guest_number_comes_from_the_source(self):
        from unittest.mock import patch

        fake = self._fake_source([])
        with patch("tournaments.views.get_player_source", return_value=fake):
            self._guest_post(name="Sourced Sam", payment_note="")

        player = Player.objects.get(name="Sourced Sam")
        self.assertEqual(
            player.player_number, "X-1",
            "the source mints the number, not next_temp_player_number",
        )


class WespaGuestTests(RegistrationTestCase):
    """Entering somebody who exists only in the WESPA list.

    The flow the whole WESPA mirror is for (``plans/PLAN_WESPA.md`` phase 5): an
    overseas visitor has no CoCo number, so the player search can never find
    them, and their rating used to be typed in at the desk from a website.
    """

    def setUp(self):
        super().setUp()
        from tournaments.models import WespaPlayer

        self.row = WespaPlayer.objects.create(
            wespa_id=7, name="Nadia Sharma", country="IND", rating=1750
        )

    def test_the_search_offers_wespa_players_baxter_does_not_have(self):
        response = self.client.get(self.url(), {"q": "Nadia"})
        self.assertEqual(
            [r.wespa_id for r in response.context["wespa_results"]], [7]
        )
        self.assertContains(response, "Enter as guest")

    def test_a_row_already_linked_is_not_offered_twice(self):
        """They are in the player results, under the number they will be entered on."""
        Player.objects.create(
            name="Nadia Sharma", player_number="T-9", rating=0, wespa_id=7
        )
        response = self.client.get(self.url(), {"q": "Nadia"})
        self.assertEqual(response.context["wespa_results"], [])

    def test_entering_mints_a_linked_guest_seeded_from_wespa(self):
        response = self._post(action="add", wespa="7", number=1)
        self.assertContains(response, "Entered Nadia Sharma")
        player = Player.objects.get(name="Nadia Sharma")
        self.assertEqual(player.wespa_id, 7)
        self.assertEqual(player.wespa_rating, 1750)
        self.assertEqual(player.rating, 0)
        self.assertTrue(player.is_provisional)
        entrant = self.division.entrants.get(player=player)
        self.assertEqual((entrant.rating, entrant.rating_source), (1750, "wespa"))

    def test_the_registration_fields_apply_as_for_anyone_else(self):
        self._post(action="add", wespa="7", tentative="on")
        entrant = self.division.entrants.get(player__wespa_id=7)
        self.assertTrue(entrant.tentative)

    def test_a_second_division_reuses_the_player_rather_than_minting_one(self):
        self._post(action="add", wespa="7", number=1)
        other = Division.objects.create(tournament=self.tournament, name="B")
        DivisionSettings.objects.create(division=other)
        self.client.post(
            reverse("division_register", kwargs=other.slug_kwargs()),
            {"action": "add", "wespa": "7", "number": 1},
            follow=True,
        )
        self.assertEqual(Player.objects.filter(wespa_id=7).count(), 1)

    def test_the_creation_is_logged_with_the_link(self):
        """So a replay recreates the guest already linked, not as a bare name."""
        from tournaments.models import TournamentEvent

        self._post(action="add", wespa="7", number=1)
        event = TournamentEvent.objects.get(
            tournament=self.tournament, event_type="player_created"
        )
        self.assertEqual(event.payload["wespa_id"], 7)
        self.assertEqual(event.payload["wespa_rating"], 1750)

    def test_an_unknown_row_is_a_message_not_a_crash(self):
        response = self._post(action="add", wespa="999", number=1)
        self.assertContains(response, "no longer listed")
        self.assertEqual(self.division.entrants.count(), 0)


class SeedingTests(RegistrationTestCase):
    """Entrant numbers are a seeding, derived from the rating and never typed.

    An entrant's ``number`` is their number *for this tournament* — not a seat,
    not a board — so it follows the pinned rating while the division is still in
    draft, and freezes once a round has been published.
    """

    def _numbers(self):
        return {
            e.player.name: e.number
            for e in self.division.entrants.select_related("player")
        }

    def _publish_a_round(self):
        from tournaments.models import RoundPairings

        RoundPairings.objects.create(
            division=self.division, round=1, status=RoundPairings.PUBLISHED
        )

    def test_the_form_no_longer_asks_for_a_number(self):
        from tournaments.forms import RegistrationForm

        self.assertNotIn("number", RegistrationForm().fields)

    def test_entering_orders_the_division_by_rating(self):
        # Entered lowest-rated first, on purpose: the numbers must not follow
        # entry order.
        self._post(action="add", player=self.unrated.player_number)   # 0
        self._post(action="add", player=self.bea.player_number)       # 1400 WESPA
        self._post(action="add", player=self.ann.player_number)       # 1600 CoCo
        self.assertEqual(
            self._numbers(), {"Ann Lee": 1, "Bea Fox": 2, "Cy Ray": 3}
        )

    def test_a_rating_correction_reorders_the_field(self):
        self._post(action="add", player=self.ann.player_number)
        self._post(action="add", player=self.bea.player_number)
        entrant = self.division.entrants.get(player=self.bea)
        self._post(action="update", entrant=entrant.pk, rating=1900)
        self.assertEqual(self._numbers(), {"Bea Fox": 1, "Ann Lee": 2})

    def test_a_tie_breaks_on_the_player_number(self):
        """A tie has to break on something replayable — not on row order."""
        self._post(action="add", player=self.unrated.player_number)  # 0003
        other = Player.objects.create(
            name="Dee Vee", player_number="0004", rating=0
        )
        self._post(action="add", player=other.player_number)
        self.assertEqual(self._numbers(), {"Cy Ray": 1, "Dee Vee": 2})

    def test_a_late_entrant_is_appended_once_a_round_is_published(self):
        """The seeding is what the division started as; it does not reshuffle."""
        self._post(action="add", player=self.ann.player_number)
        self._post(action="add", player=self.unrated.player_number)
        before = self._numbers()
        self._publish_a_round()

        self._post(action="add", player=self.bea.player_number)  # would seed 2nd

        self.assertEqual(self._numbers(), {**before, "Bea Fox": 3})

    def test_a_rating_correction_under_way_moves_nobody(self):
        self._post(action="add", player=self.ann.player_number)
        self._post(action="add", player=self.bea.player_number)
        before = self._numbers()
        self._publish_a_round()

        entrant = self.division.entrants.get(player=self.bea)
        self._post(action="update", entrant=entrant.pk, rating=1900)

        self.assertEqual(self._numbers(), before)

    def test_the_renumber_is_logged_with_the_numbers_it_wrote(self):
        """Not "sort by rating" — a payload meaning that would replay against a
        different rating table and renumber differently."""
        from tournaments.models import TournamentEvent

        self._post(action="add", player=self.unrated.player_number)
        self._post(action="add", player=self.ann.player_number)
        event = TournamentEvent.objects.filter(
            tournament=self.tournament, event_type="entrants_reseeded"
        ).latest("seq")
        self.assertEqual(event.payload["seeding"], [["0001", 1], ["0003", 2]])

    def test_a_renumber_that_moves_nobody_is_not_logged(self):
        """Every add calls it, so a no-op must not fill the log."""
        from tournaments.models import TournamentEvent

        self._post(action="add", player=self.ann.player_number)
        self.assertFalse(
            TournamentEvent.objects.filter(
                tournament=self.tournament, event_type="entrants_reseeded"
            ).exists()
        )

    def test_the_added_entrant_records_the_number_it_got(self):
        """The payload is explicit even though the form supplied nothing."""
        from tournaments.models import TournamentEvent

        self._post(action="add", player=self.ann.player_number)
        event = TournamentEvent.objects.get(
            tournament=self.tournament, event_type="entrant_added"
        )
        self.assertEqual(event.payload["number"], 1)


class UnifiedAddTests(RegistrationTestCase):
    """One search, three outcomes — the whole point of the unified form.

    A director types a name and takes whoever comes back: a CoCo player entered
    on their own number, a WESPA-only player entered as a guest in one click, or
    a stranger entered as a guest with a rating the director types.
    """

    def setUp(self):
        super().setUp()
        from tournaments.models import WespaPlayer

        WespaPlayer.objects.create(
            wespa_id=7, name="Nadia Sharma", country="IND", rating=1750
        )

    def test_one_search_offers_both_lists(self):
        self.ann.name = "Nadia Lee"
        self.ann.save(update_fields=["name"])
        response = self.client.get(self.url(), {"q": "Nadia"})
        self.assertEqual(
            [r.player_number for r in response.context["search_results"]], ["0001"]
        )
        self.assertEqual(
            [r.wespa_id for r in response.context["wespa_results"]], [7]
        )

    def test_the_guest_name_is_prefilled_from_the_search(self):
        """Re-typing it at a busy desk is how a visitor ends up as "Nadia"."""
        response = self.client.get(self.url(), {"q": "  Nadia Sharma  "})
        self.assertEqual(
            response.context["guest_form"].initial["name"], "Nadia Sharma"
        )

    def test_the_guest_branch_only_appears_once_a_search_has_run(self):
        self.assertNotContains(self.client.get(self.url()), "Add as guest")
        self.assertContains(
            self.client.get(self.url(), {"q": "nobody"}), "Add as guest"
        )

    def test_the_three_branches_share_one_registration_fieldset(self):
        """It used to be rendered twice, which is why the guest copy was
        prefixed. One form, one fieldset, no prefix."""
        page = self.client.get(self.url(), {"q": "x"}).content.decode()
        self.assertEqual(page.count('name="payment_note"'), 1)
        self.assertNotIn("guest-payment_note", page)

    def test_a_coco_hit_enters_the_existing_player(self):
        self._post(action="add", player=self.ann.player_number)
        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player, self.ann)
        self.assertEqual(entrant.rating_source, "coco")

    def test_a_wespa_hit_enters_a_linked_guest_in_one_click(self):
        self._post(action="add", wespa="7")
        player = Player.objects.get(name="Nadia Sharma")
        self.assertEqual((player.wespa_id, player.wespa_rating), (7, 1750))
        self.assertEqual(
            self.division.entrants.get().rating_source, "wespa"
        )

    def test_a_stranger_is_a_guest_with_a_manual_rating(self):
        self._guest_post(name="Walk In", rating=1234)
        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player.name, "Walk In")
        self.assertEqual((entrant.rating, entrant.rating_source), (1234, "manual"))

    def test_the_button_pressed_picks_the_branch_not_a_hidden_action(self):
        """All three post action=add; only the button's own name differs."""
        self._post(action="add", wespa="7")
        self._guest_post(name="Walk In", rating=1234)
        self._post(action="add", player=self.ann.player_number)
        self.assertEqual(
            set(self.division.entrants.values_list("player__name", flat=True)),
            {"Nadia Sharma", "Walk In", "Ann Lee"},
        )

    def test_the_fields_are_hidden_until_there_is_somebody_to_apply_them_to(self):
        page = self.client.get(self.url()).content.decode()
        self.assertNotIn('name="payment_note"', page)
        self.assertIn('name="q"', page)

    def test_a_rejected_guest_comes_back_with_its_fields_and_errors(self):
        """The fields are gated on the search, so a bare error page would
        otherwise lose the form the director was filling in."""
        response = self._guest_post(name="", rating=1400)
        page = response.content.decode()
        self.assertIn('name="payment_note"', page)
        self.assertIn("Add as guest", page)
        self.assertEqual(self.division.entrants.count(), 0)


class DuplicateGuestNameTests(RegistrationTestCase):
    """A guest whose name is already taken is a question, not a twin.

    Sharing a name is legal — the player number is the identity — but it is far
    more often a typo than two real people. The search is the first guard and
    catches most of it; this catches a director who searched one thing and typed
    another, which is when it actually happens.
    """

    def test_an_unconfirmed_duplicate_creates_nobody(self):
        response = self._guest_post(name="Ann Lee", rating=1400)
        self.assertEqual(Player.objects.filter(name="Ann Lee").count(), 1)
        self.assertEqual(self.division.entrants.count(), 0)
        self.assertEqual(
            [p.player_number for p in response.context["duplicate_candidates"]],
            ["0001"],
        )
        self.assertContains(response, "Did you mean them?")

    def test_the_check_is_case_insensitive(self):
        response = self._guest_post(name="  ann lee  ", rating=1400)
        self.assertTrue(response.context["duplicate_candidates"])

    def test_confirming_creates_a_second_person_on_their_own_number(self):
        self._post(action="add", guest="confirm", name="Ann Lee", rating=1400)
        both = Player.objects.filter(name__iexact="Ann Lee")
        self.assertEqual(both.count(), 2)
        self.assertEqual(len({p.player_number for p in both}), 2)
        entrant = self.division.entrants.get()
        self.assertTrue(entrant.player.is_provisional)
        self.assertEqual((entrant.rating, entrant.rating_source), (1400, "manual"))

    def test_picking_the_existing_player_is_the_ordinary_add(self):
        """The candidate button posts their number, like a search result's."""
        self._post(action="add", player="0001", name="Ann Lee")
        self.assertEqual(Player.objects.filter(name="Ann Lee").count(), 1)
        entrant = self.division.entrants.get()
        self.assertEqual(entrant.player, self.ann)
        self.assertEqual(entrant.rating_source, "coco")

    def test_a_name_nobody_has_is_not_questioned(self):
        self._guest_post(name="Wholly New", rating=1400)
        self.assertEqual(self.division.entrants.count(), 1)

    def test_the_typed_rating_survives_the_question(self):
        """"Add as a different person" resubmits this form, so a fieldset that
        is not rendered would silently drop what the director typed."""
        response = self._guest_post(name="Ann Lee", rating=1400, tentative="on")
        page = response.content.decode()
        self.assertIn('value="1400"', page)
        self.assertNotIn("Nobody found", page)
        self.assertIn('name="payment_note"', page)
