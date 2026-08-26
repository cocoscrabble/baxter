"""Pulling the central roster (plans/PLAN_COCO_PROGRAM.md, the "before" half).

The point of the pull is that Baxter can then run a whole tournament with no
connection to the central database — so what matters is that it lands the *full*
rating seed, and that it cannot disturb a tournament already under way.
"""

import json
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
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
