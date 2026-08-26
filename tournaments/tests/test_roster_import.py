"""Pulling the central roster (plans/PLAN_COCO_PROGRAM.md, the "before" half).

The point of the pull is that Baxter can then run a whole tournament with no
connection to the central database — so what matters is that it lands the *full*
rating seed, and that it cannot disturb a tournament already under way.
"""

import json
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from tournaments.models import Division, Entrant, Player, Tournament
from tournaments.roster_import import (
    RosterParseError,
    import_roster,
    parse_roster,
)
from users.models import User


def roster(*players, generated_at="2026-08-22T14:03:00Z"):
    return {
        "schema": "coco.roster/1",
        "generated_at": generated_at,
        "players": list(players),
    }


def entry(number, name, rating=1600, deviation=80.0, games=100,
          last_played="2026-03-14"):
    return {
        "player_number": number,
        "name": name,
        "rating": rating,
        "deviation": deviation,
        "career_games": games,
        "last_played": last_played,
    }


class ParseTests(TestCase):
    def test_it_reads_bytes_str_and_dict_alike(self):
        doc = roster(entry("0233", "Alec"))
        for raw in (doc, json.dumps(doc), json.dumps(doc).encode()):
            _, rows = parse_roster(raw)
            self.assertEqual(rows[0]["player_number"], "0233")

    def test_a_bom_does_not_break_it(self):
        raw = json.dumps(roster(entry("0233", "Alec"))).encode("utf-8-sig")
        _, rows = parse_roster(raw)
        self.assertEqual(rows[0]["name"], "Alec")

    def test_numbers_are_canonicalized(self):
        _, rows = parse_roster(roster(entry("7", "Nellie")))
        self.assertEqual(rows[0]["player_number"], "0007")

    def test_a_null_rating_becomes_zero(self):
        """Baxter has always spelled 'no CoCo rating' as 0."""
        _, rows = parse_roster(
            roster(entry("7", "Nellie", rating=None, deviation=None,
                         games=0, last_played=None))
        )
        self.assertEqual(rows[0]["rating"], 0)
        self.assertIsNone(rows[0]["deviation"])
        self.assertIsNone(rows[0]["last_played"])

    def test_an_unknown_schema_is_refused_rather_than_half_read(self):
        doc = roster(entry("0233", "Alec"))
        doc["schema"] = "coco.roster/2"
        with self.assertRaisesRegex(RosterParseError, "coco.roster/2"):
            parse_roster(doc)

    def test_malformed_json_says_so(self):
        with self.assertRaisesRegex(RosterParseError, "Not valid JSON"):
            parse_roster("{nope")

    def test_a_missing_players_list_says_so(self):
        with self.assertRaisesRegex(RosterParseError, "no 'players' list"):
            parse_roster({"schema": "coco.roster/1"})

    def test_a_row_with_no_number_says_which_row(self):
        with self.assertRaisesRegex(RosterParseError, "Player 1"):
            parse_roster(roster({"name": "Nameless"}))

    def test_a_bad_date_names_the_player(self):
        with self.assertRaisesRegex(RosterParseError, "#0233"):
            parse_roster(roster(entry("0233", "Alec", last_played="last tuesday")))


class ImportTests(TestCase):
    def test_a_new_player_is_created_with_the_whole_seed(self):
        result = import_roster(roster(entry("0233", "Alec Sjöholm")))
        self.assertEqual(result.added, ["0233"])
        player = Player.objects.get(player_number="0233")
        self.assertEqual(player.name, "Alec Sjöholm")
        self.assertEqual(player.rating, 1600)
        self.assertEqual(player.deviation, 80.0)
        self.assertEqual(player.career_games, 100)
        self.assertEqual(player.last_played, date(2026, 3, 14))
        self.assertFalse(player.is_provisional)

    def test_an_existing_player_is_updated_in_place(self):
        player = Player.objects.create(
            player_number="0233", name="Old Name", rating=1500
        )
        result = import_roster(roster(entry("0233", "Alec")))
        self.assertEqual(result.updated, ["0233"])
        player.refresh_from_db()
        self.assertEqual(player.name, "Alec")
        self.assertEqual(player.rating, 1600)
        # Same row, so entrants and results that reference it are untouched.
        self.assertEqual(Player.objects.filter(player_number="0233").count(), 1)

    def test_an_unchanged_player_is_reported_as_such(self):
        import_roster(roster(entry("0233", "Alec")))
        result = import_roster(roster(entry("0233", "Alec")))
        self.assertEqual(result.unchanged, ["0233"])
        self.assertEqual(result.updated, [])

    def test_a_bare_number_updates_a_padded_row(self):
        Player.objects.create(player_number="0233", name="Alec", rating=1500)
        import_roster(roster(entry("233", "Alec")))
        self.assertEqual(Player.objects.filter(name="Alec").count(), 1)

    def test_matching_is_on_the_number_never_the_name(self):
        """Two players share a name; only the numbered one moves."""
        a = Player.objects.create(player_number="0010", name="John Smith", rating=1500)
        b = Player.objects.create(player_number="0011", name="John Smith", rating=1300)
        import_roster(roster(entry("0011", "John Smith", rating=1777)))
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.rating, 1500)
        self.assertEqual(b.rating, 1777)

    def test_a_pull_clears_provisional(self):
        """A player the central database knows is not provisional any more."""
        Player.objects.create(
            player_number="0233", name="Alec", rating=1600,
            deviation=80.0, career_games=100, last_played=date(2026, 3, 14),
            is_provisional=True,
        )
        result = import_roster(roster(entry("0233", "Alec")))
        self.assertEqual(result.updated, ["0233"])
        self.assertFalse(Player.objects.get(player_number="0233").is_provisional)

    def test_a_guest_the_roster_never_heard_of_is_left_alone(self):
        guest = Player.objects.create(
            player_number="T-7", name="Gwen Guest", rating=0,
            wespa_rating=1450, is_provisional=True,
        )
        import_roster(roster(entry("0233", "Alec")))
        guest.refresh_from_db()
        self.assertTrue(guest.is_provisional)
        self.assertEqual(guest.wespa_rating, 1450)
        self.assertTrue(Player.objects.filter(player_number="T-7").exists())

    def test_a_pull_never_touches_the_wespa_rating(self):
        """It is not the central database's to know, and a pull must not clear
        one somebody uploaded."""
        Player.objects.create(
            player_number="0233", name="Alec", rating=1500, wespa_rating=1780
        )
        import_roster(roster(entry("0233", "Alec")))
        self.assertEqual(
            Player.objects.get(player_number="0233").wespa_rating, 1780
        )

    def test_the_bye_is_not_a_candidate(self):
        bye = Player.get_bye()
        import_roster(roster(entry("0233", "Alec")))
        bye.refresh_from_db()
        self.assertTrue(bye.is_bye)
        self.assertEqual(bye.player_number, "BYE")

    def test_a_malformed_document_changes_nothing(self):
        Player.objects.create(player_number="0233", name="Old", rating=1500)
        doc = roster(entry("0233", "Alec"), entry("0234", "Bad", last_played="nope"))
        with self.assertRaises(RosterParseError):
            import_roster(doc)
        self.assertEqual(Player.objects.get(player_number="0233").name, "Old")
        self.assertFalse(Player.objects.filter(player_number="0234").exists())

    def test_the_generated_at_stamp_comes_back(self):
        result = import_roster(roster(entry("0233", "Alec")))
        self.assertEqual(result.generated_at, "2026-08-22T14:03:00Z")


class SeedFreezingTests(TestCase):
    """The reason a pull is safe to run at any time (decision 6)."""

    def setUp(self):
        owner = User.objects.create_user(username="td-roster", password="pw")
        self.tournament = Tournament.objects.create(
            name="Live", location="X", start_date=date(2026, 5, 1), owner=owner
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )

    def test_an_entrant_freezes_the_whole_seed_not_just_the_rating(self):
        import_roster(roster(entry("0233", "Alec")))
        player = Player.objects.get(player_number="0233")
        entrant = Entrant.enter(self.division, player, 1)
        self.assertEqual(entrant.rating, 1600)
        self.assertEqual(entrant.deviation, 80.0)
        self.assertEqual(entrant.career_games, 100)
        self.assertEqual(entrant.last_played, date(2026, 3, 14))

    def test_a_later_pull_cannot_move_a_running_tournament(self):
        import_roster(roster(entry("0233", "Alec")))
        player = Player.objects.get(player_number="0233")
        entrant = Entrant.enter(self.division, player, 1)

        import_roster(
            roster(entry("0233", "Alec", rating=1900, deviation=60.0,
                         games=200, last_played="2026-08-01"))
        )

        entrant.refresh_from_db()
        self.assertEqual(entrant.rating, 1600)
        self.assertEqual(entrant.deviation, 80.0)
        self.assertEqual(entrant.career_games, 100)
        self.assertEqual(entrant.last_played, date(2026, 3, 14))
        # …while the player record has moved on, which is the point.
        player.refresh_from_db()
        self.assertEqual(player.rating, 1900)

    def test_an_unrated_player_freezes_as_unrated(self):
        import_roster(
            roster(entry("0007", "Nellie", rating=None, deviation=None,
                         games=0, last_played=None))
        )
        player = Player.objects.get(player_number="0007")
        entrant = Entrant.enter(self.division, player, 1)
        self.assertEqual((entrant.rating, entrant.rating_source), (0, "none"))
        self.assertEqual(entrant.deviation, 0.0)
        self.assertEqual(entrant.career_games, 0)
        self.assertIsNone(entrant.last_played)

    def test_a_hand_set_rating_does_not_rewrite_the_playing_history(self):
        """A director saying what someone is worth is not a claim about how many
        games they have played."""
        import_roster(roster(entry("0233", "Alec")))
        player = Player.objects.get(player_number="0233")
        entrant = Entrant.enter(self.division, player, 1, rating=1234)
        self.assertEqual((entrant.rating, entrant.rating_source), (1234, "manual"))
        self.assertEqual(entrant.career_games, 100)
        self.assertEqual(entrant.last_played, date(2026, 3, 14))


class RosterImportViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="pw", role="admin", is_staff=True
        )
        self.director = User.objects.create_user(
            username="td3", password="pw", role="director"
        )
        self.url = reverse("roster_import")

    def _upload(self, doc):
        return self.client.post(
            self.url,
            {
                "roster_file": SimpleUploadedFile(
                    "roster.json", json.dumps(doc).encode(), "application/json"
                )
            },
            follow=True,
        )

    def test_an_admin_can_pull(self):
        self.client.force_login(self.admin)
        response = self._upload(roster(entry("0233", "Alec")))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Player.objects.filter(player_number="0233").exists())
        self.assertContains(response, "1 added")

    def test_a_director_cannot(self):
        self.client.force_login(self.director)
        self._upload(roster(entry("0233", "Alec")))
        self.assertFalse(Player.objects.filter(player_number="0233").exists())

    def test_a_bad_document_is_reported_not_crashed(self):
        self.client.force_login(self.admin)
        doc = roster(entry("0233", "Alec"))
        doc["schema"] = "something/else"
        response = self._upload(doc)
        self.assertContains(response, "Unsupported roster schema")
        self.assertFalse(Player.objects.filter(player_number="0233").exists())


class FetchTests(TestCase):
    """Pulling over HTTP — the normal path (the file upload is the offline one).

    Errors are translated because the raw ones are not actionable: a bare
    ``HTTPError: 401`` does not say "check the token".
    """

    URL = "https://cocodb.example/api/roster/"

    def _fetch(self, **patches):
        from unittest.mock import patch

        from tournaments.roster_import import fetch_roster

        with patch("tournaments.roster_import.urllib.request.urlopen", **patches):
            return fetch_roster()

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_it_sends_the_bearer_token_and_returns_the_body(self):
        from unittest.mock import MagicMock, patch

        from tournaments.roster_import import fetch_roster

        body = json.dumps(roster(entry("0233", "Alec"))).encode()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body
        with patch(
            "tournaments.roster_import.urllib.request.urlopen", return_value=response
        ) as opener:
            self.assertEqual(fetch_roster(), body)

        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, self.URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    @override_settings(ROSTER_API_URL="", ROSTER_API_TOKEN="")
    def test_an_unconfigured_endpoint_says_what_to_set(self):
        from tournaments.roster_import import RosterFetchError, fetch_roster

        with self.assertRaisesRegex(RosterFetchError, "ROSTER_API_URL"):
            fetch_roster()

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_a_rejected_token_says_so(self):
        import urllib.error

        from tournaments.roster_import import RosterFetchError

        with self.assertRaisesRegex(RosterFetchError, "rejected the token"):
            self._fetch(
                side_effect=urllib.error.HTTPError(
                    self.URL, 401, "Unauthorized", {}, None
                )
            )

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_another_http_error_reports_its_code(self):
        import urllib.error

        from tournaments.roster_import import RosterFetchError

        with self.assertRaisesRegex(RosterFetchError, "500"):
            self._fetch(
                side_effect=urllib.error.HTTPError(
                    self.URL, 500, "Server Error", {}, None
                )
            )

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_an_unreachable_host_names_the_address(self):
        import urllib.error

        from tournaments.roster_import import RosterFetchError

        with self.assertRaisesRegex(RosterFetchError, "cocodb.example"):
            self._fetch(side_effect=urllib.error.URLError("nodename nor servname"))

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_a_timeout_says_how_long_it_waited(self):
        from tournaments.roster_import import RosterFetchError

        with self.assertRaisesRegex(RosterFetchError, "30 seconds"):
            self._fetch(side_effect=TimeoutError())


class FetchViewTests(TestCase):
    URL = "https://cocodb.example/api/roster/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="pw", role="admin", is_staff=True
        )
        self.client.force_login(self.admin)
        self.url = reverse("roster_import")

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_the_fetch_button_appears_when_configured(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Pull now")
        self.assertContains(response, "cocodb.example")

    @override_settings(ROSTER_API_URL="", ROSTER_API_TOKEN="")
    def test_without_configuration_only_the_upload_is_offered(self):
        response = self.client.get(self.url)
        self.assertNotContains(response, "Pull now")
        self.assertContains(response, "No roster endpoint is configured")

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_a_fetch_imports_the_same_way_an_upload_does(self):
        from unittest.mock import patch

        body = json.dumps(roster(entry("0233", "Alec"))).encode()
        with patch("tournaments.views.fetch_roster", return_value=body):
            response = self.client.post(self.url, {"source": "fetch"}, follow=True)

        self.assertContains(response, "1 added")
        self.assertTrue(Player.objects.filter(player_number="0233").exists())

    @override_settings(ROSTER_API_URL=URL, ROSTER_API_TOKEN="tok")
    def test_a_failed_fetch_is_reported_not_crashed(self):
        from unittest.mock import patch

        from tournaments.roster_import import RosterFetchError

        with patch(
            "tournaments.views.fetch_roster",
            side_effect=RosterFetchError("The central database rejected the token."),
        ):
            response = self.client.post(self.url, {"source": "fetch"}, follow=True)

        self.assertContains(response, "rejected the token")
        self.assertFalse(Player.objects.filter(player_number="0233").exists())


class HeldBackTests(TestCase):
    """A roster row that looks like a local guest is not created.

    This is the case that motivated the whole feature: a guest plays under a
    ``T-`` number, an admin gives them a real one centrally, and the next pull
    would otherwise create a *second* copy of them — one holding the entrants
    and results, one holding the number — leaving the entrant as unexportable as
    before, and a duplicate to clean up.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="td-held", password="pw")
        self.tournament = Tournament.objects.create(
            name="Champs", location="X",
            start_date=date(2026, 5, 1), owner=self.owner,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )
        self.entrant = Entrant.enter(self.division, self.guest, 1)

    def test_the_pull_holds_it_back_and_creates_nothing(self):
        result = import_roster(roster(entry("0301", "Joe Thorngren")))
        self.assertEqual(result.added, [])
        self.assertEqual(len(result.pending), 1)

        held = result.pending[0]
        self.assertEqual(held.local_number, "T-4")
        self.assertEqual(held.roster_number, "0301")
        # Nothing written: still one Joe, still provisional, still on T-4.
        self.assertEqual(Player.objects.filter(name="Joe Thorngren").count(), 1)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")
        self.assertTrue(self.guest.is_provisional)

    def test_two_guests_sharing_a_name_are_not_matched(self):
        """Exactly the case a human has to disambiguate."""
        Player.objects.create(
            name="Joe Thorngren", player_number="T-9", rating=0,
            is_provisional=True,
        )
        result = import_roster(roster(entry("0301", "Joe Thorngren")))
        self.assertEqual(result.pending, [])
        self.assertEqual(result.added, ["0301"])

    def test_a_settled_player_with_the_same_name_is_two_people(self):
        """Both have real numbers, so they are not the same human and the
        roster is right to add the one Baxter has not seen."""
        Player.objects.create(
            name="Ada Real", player_number="0500", rating=1500,
            is_provisional=False,
        )
        result = import_roster(roster(entry("0600", "Ada Real")))
        self.assertEqual(result.pending, [])
        self.assertEqual(result.added, ["0600"])

    def test_a_guest_the_roster_still_does_not_know_is_untouched(self):
        result = import_roster(roster(entry("0999", "Someone Else")))
        self.assertEqual(result.pending, [])
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")


class ResolutionTests(TestCase):
    """Confirming a held-back rename."""

    def setUp(self):
        self.owner = User.objects.create_user(username="td-res", password="pw")
        from tournaments.commands import create_tournament

        self.tournament = create_tournament(
            None, self.owner,
            {
                "name": "Champs", "location": "X", "start_date": "2026-05-01",
                "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
            },
        )
        self.division = self.tournament.divisions.get()
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )
        self.entrant = Entrant.enter(self.division, self.guest, 1)

    def _pending(self):
        result = import_roster(
            roster(entry("0301", "Joe Thorngren", rating=1400,
                         deviation=90.0, games=12, last_played="2026-08-01"))
        )
        return result.pending[0]

    def test_it_renames_in_place_and_keeps_everything(self):
        from tournaments.roster_import import resolve_number

        resolve_number(self._pending(), actor=self.owner)

        # One player, on the new number, no longer provisional.
        self.assertEqual(Player.objects.filter(name="Joe Thorngren").count(), 1)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "0301")
        self.assertFalse(self.guest.is_provisional)
        # …and carrying the registry's data.
        self.assertEqual(self.guest.rating, 1400)
        self.assertEqual(self.guest.career_games, 12)
        self.assertEqual(self.guest.last_played, date(2026, 8, 1))
        # The entrant is the same row, so results and pairings are untouched.
        self.entrant.refresh_from_db()
        self.assertEqual(self.entrant.player_id, self.guest.pk)
        self.assertEqual(self.entrant.key, "0301")

    def test_the_pinned_rating_does_not_move(self):
        """A rename is not a reason to reseed a tournament already under way."""
        from tournaments.roster_import import resolve_number

        before = (self.entrant.rating, self.entrant.rating_source)
        resolve_number(self._pending(), actor=self.owner)
        self.entrant.refresh_from_db()
        self.assertEqual((self.entrant.rating, self.entrant.rating_source), before)

    def test_it_is_logged(self):
        from tournaments.roster_import import resolve_number

        resolve_number(self._pending(), actor=self.owner)
        event = self.tournament.events.get(event_type="player_number_changed")
        self.assertEqual(event.payload, {"old": "T-4", "new": "0301"})

    def test_it_is_logged_in_every_tournament_the_player_played_in(self):
        from tournaments.commands import create_tournament
        from tournaments.roster_import import resolve_number

        second = create_tournament(
            None, self.owner,
            {
                "name": "Other", "location": "X", "start_date": "2026-06-01",
                "editors": [], "default_division": {"name": "Open", "pairing_seed": 1},
            },
        )
        Entrant.enter(second.divisions.get(), self.guest, 1)

        resolve_number(self._pending(), actor=self.owner)

        for tournament in (self.tournament, second):
            with self.subTest(tournament=tournament.name):
                self.assertEqual(
                    tournament.events.filter(
                        event_type="player_number_changed"
                    ).count(),
                    1,
                    "each log names this player by number, so each needs the change",
                )

    def test_a_taken_number_is_refused(self):
        from tournaments.roster_import import RosterParseError, resolve_number

        pending = self._pending()
        Player.objects.create(name="Someone", player_number="0301", rating=1500)
        with self.assertRaisesRegex(RosterParseError, "already belongs"):
            resolve_number(pending, actor=self.owner)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")

    def test_a_stale_resolution_is_refused(self):
        from tournaments.roster_import import RosterParseError, resolve_number

        pending = self._pending()
        self.guest.player_number = "T-99"
        self.guest.save(update_fields=["player_number"])
        with self.assertRaisesRegex(RosterParseError, "no longer on"):
            resolve_number(pending, actor=self.owner)

    def test_a_player_with_no_tournaments_is_renamed_without_an_event(self):
        """There is no log for it to belong to."""
        from tournaments.roster_import import resolve_number

        self.entrant.delete()
        resolve_number(self._pending(), actor=self.owner)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "0301")
        self.assertEqual(
            self.tournament.events.filter(
                event_type="player_number_changed"
            ).count(),
            0,
        )


class ResolutionViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin-res", password="pw", role="admin", is_staff=True
        )
        self.client.force_login(self.admin)
        self.url = reverse("roster_import")
        self.guest = Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0,
            is_provisional=True,
        )

    def _pull(self):
        return self.client.post(
            self.url,
            {
                "roster_file": SimpleUploadedFile(
                    "roster.json",
                    json.dumps(roster(entry("0301", "Joe Thorngren"))).encode(),
                    "application/json",
                )
            },
            follow=True,
        )

    def test_a_pull_offers_the_resolution_rather_than_applying_it(self):
        response = self._pull()
        self.assertContains(response, "Needs confirming")
        self.assertContains(response, "T-4")
        self.assertContains(response, "0301")
        self.assertContains(response, "look like guests")
        self.assertEqual(Player.objects.filter(name="Joe Thorngren").count(), 1)

    def test_confirming_applies_it(self):
        self._pull()
        response = self.client.post(
            self.url, {"action": "resolve", "pending": "T-4:0301"}, follow=True
        )
        self.assertContains(response, "now #0301")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "0301")
        # …and it stops being offered.
        self.assertNotContains(response, "Needs confirming")

    def test_declining_leaves_everything_alone(self):
        self._pull()
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")
        self.assertTrue(self.guest.is_provisional)

    def test_an_unknown_resolution_is_reported(self):
        response = self.client.post(
            self.url, {"action": "resolve", "pending": "T-9:9999"}, follow=True
        )
        self.assertContains(response, "no longer pending")

    def test_a_director_cannot_resolve(self):
        director = User.objects.create_user(
            username="td-only", password="pw", role="director"
        )
        self.client.force_login(director)
        self.client.post(
            self.url, {"action": "resolve", "pending": "T-4:0301"}
        )
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.player_number, "T-4")
