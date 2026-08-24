"""What-if import: parsers (JSON bundle / coco-ratings CSV), the
division_imported command (derived pairings + inferred byes), and a replay
round-trip."""

import json

from django.test import TestCase

from tournaments.commands import create_tournament, import_division
from tournaments.events import division_digest
from tournaments.models import Player, RoundPairings, Tournament
from tournaments.replay import events_from_tournament, replay
from tournaments.whatif_import import ImportParseError, parse_import
from users.models import User


def _bundle(divisions, name="Nationals 2019"):
    players = [
        {"player_number": "P1", "name": "Alice", "rating": 1600, "provisional": False},
        {"player_number": "P2", "name": "Bob", "rating": 1500, "provisional": False},
        {"player_number": "P3", "name": "Cara", "rating": 1400, "provisional": False},
    ]
    return json.dumps({
        "name": name, "location": "Reno", "start_date": "2019-07-04",
        "players": players, "divisions": divisions, "event_log": [],
    })


class ParseJsonBundleTests(TestCase):
    def test_parses_entrants_and_results(self):
        bundle = _bundle([{
            "name": "Open",
            "entrants": [
                {"number": 1, "player_number": "P1"},
                {"number": 2, "player_number": "P2"},
            ],
            "results": [{
                "round": 1, "winner": "P1", "winner_score": 420,
                "loser": "P2", "loser_score": 388, "winner_started": True,
            }],
        }])
        name, divisions = parse_import(bundle)
        self.assertEqual(name, "Nationals 2019")
        self.assertEqual(len(divisions), 1)
        div = divisions[0]
        self.assertEqual(div["name"], "Open")
        self.assertEqual(
            div["entrants"],
            [{"player": "Alice", "rating": 1600, "number": 1},
             {"player": "Bob", "rating": 1500, "number": 2}],
        )
        self.assertEqual(div["results"][0]["winner"], "Alice")
        self.assertEqual(div["results"][0]["loser"], "Bob")

    def test_unknown_player_reference_errors(self):
        bundle = _bundle([{
            "name": "Open",
            "entrants": [{"number": 1, "player_number": "P9"}],
            "results": [],
        }])
        with self.assertRaises(ImportParseError):
            parse_import(bundle)

    def test_malformed_json_errors(self):
        with self.assertRaises(ImportParseError):
            parse_import("{ not valid json ")


class ParseCsvTests(TestCase):
    def setUp(self):
        Player.objects.create(name="Alice", player_number="1", rating=1600)
        Player.objects.create(name="Bob", player_number="2", rating=1500)

    CSV = (
        "Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
        ",1,Alice,420,Bob,388\n"
        ",1,Zed,300,alice,250\n"  # Zed unknown; "alice" reuses Alice's casing
    )

    def test_ratings_from_roster_and_numbering_by_rating(self):
        name, divisions = parse_import(self.CSV)
        self.assertIsNone(name)
        div = divisions[0]
        # Alice 1600, Bob 1500, Zed unknown -> 0; numbered by rating desc.
        self.assertEqual(
            [(e["player"], e["rating"], e["number"]) for e in div["entrants"]],
            [("Alice", 1600, 1), ("Bob", 1500, 2), ("Zed", 0, 3)],
        )
        # Names canonicalize: the "alice" cell maps back to "Alice".
        self.assertEqual(div["results"][1]["loser"], "Alice")
        self.assertTrue(all(r["winner_started"] for r in div["results"]))

    def test_bad_header_errors(self):
        with self.assertRaises(ImportParseError):
            parse_import("Name,Score\nAlice,400\n")

    def test_non_integer_score_errors(self):
        bad = ("Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
               ",1,Alice,foo,Bob,388\n")
        with self.assertRaises(ImportParseError):
            parse_import(bad)


class NumberedCsvTests(TestCase):
    """The eight-column form, and why it exists.

    The legacy six-column form is still produced by a Google Form export, so
    both widths have to parse. The numbers only matter where a name does not
    resolve on its own — which is exactly what the last test here shows.
    """

    NUMBERED = (
        "Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score,"
        "Winner Number,Opponent Number\n"
        ",1,Alice,420,Bob,388,0001,0002\n"
    )

    def setUp(self):
        self.alice = Player.objects.create(
            name="Alice", player_number="1", rating=1600
        )
        self.bob = Player.objects.create(
            name="Bob", player_number="2", rating=1500
        )

    def test_the_numbered_form_parses(self):
        _, divisions = parse_import(self.NUMBERED)
        div = divisions[0]
        self.assertEqual(
            [(e["player"], e["rating"]) for e in div["entrants"]],
            [("Alice", 1600), ("Bob", 1500)],
        )
        self.assertEqual(div["results"][0]["winner"], "Alice")

    def test_the_legacy_form_still_parses(self):
        legacy = (
            "Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
            ",1,Alice,420,Bob,388\n"
        )
        _, divisions = parse_import(legacy)
        self.assertEqual(
            [e["player"] for e in divisions[0]["entrants"]], ["Alice", "Bob"]
        )

    def test_a_number_picks_the_right_one_of_two_same_named_players(self):
        """The six-column form cannot express this, which is why the columns
        were added: by name alone, the lower-rated Alice is unreachable."""
        other_alice = Player.objects.create(
            name="Alice", player_number="0900", rating=1200
        )
        numbered = (
            "Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score,"
            "Winner Number,Opponent Number\n"
            f",1,Alice,420,Bob,388,{other_alice.player_number},"
            f"{self.bob.player_number}\n"
        )
        _, divisions = parse_import(numbered)
        ratings = {e["player"]: e["rating"] for e in divisions[0]["entrants"]}
        self.assertEqual(ratings["Alice"], 1200)

        # The same file without numbers resolves to whichever Alice comes first,
        # and cannot say which was meant.
        legacy = (
            "Submitted On,Round,Winner,Winners Score,Opponent,Opponents Score\n"
            ",1,Alice,420,Bob,388\n"
        )
        _, legacy_divisions = parse_import(legacy)
        legacy_ratings = {
            e["player"]: e["rating"] for e in legacy_divisions[0]["entrants"]
        }
        self.assertNotEqual(legacy_ratings["Alice"], 1200)


class ImportDivisionCommandTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.tournament = Tournament.objects.create(
            name="What-if", location="sandbox",
            start_date="2026-07-16", owner=self.owner, is_fake=True,
        )

    def _portable(self):
        # 3 entrants; in round 1 Alice beats Bob, Cara is idle (should get a bye).
        return {
            "name": "Open",
            "entrants": [
                {"player": "Alice", "rating": 1600, "number": 1},
                {"player": "Bob", "rating": 1500, "number": 2},
                {"player": "Cara", "rating": 1400, "number": 3},
            ],
            "results": [{
                "round": 1, "winner": "Alice", "winner_score": 420,
                "loser": "Bob", "loser_score": 388, "winner_started": True,
            }],
        }

    def test_builds_finished_division_with_inferred_bye(self):
        summary = import_division(self.tournament, self.owner, self._portable())
        division = self.tournament.divisions.get(name="Open")

        self.assertTrue(division.is_test)
        self.assertEqual(division.entrants.count(), 3)
        # Round 1 is FINISHED with the real game and the inferred bye.
        rp = division.round_pairings_set.get(round=1)
        self.assertEqual(rp.status, RoundPairings.FINISHED)
        self.assertEqual(division.pairings.filter(round=1).count(), 2)

        bye_slip = division.result_slips.get(loser__player__is_bye=True)
        self.assertEqual(bye_slip.winner.player.name, "Cara")
        self.assertEqual((bye_slip.winner_score, bye_slip.loser_score), (50, 0))
        self.assertEqual(summary["inferred_byes"], [[1, "Cara"]])
        self.assertEqual(summary["entrants"], 3)

    def test_orientation_follows_winner_started(self):
        payload = self._portable()
        payload["results"][0]["winner_started"] = False  # Bob (loser) started
        import_division(self.tournament, self.owner, payload)
        division = self.tournament.divisions.get(name="Open")
        real = division.pairings.filter(round=1).exclude(
            second__player__is_bye=True
        ).get()
        # Loser started -> loser is listed first.
        self.assertEqual(real.first.player.name, "Bob")
        self.assertEqual(real.second.player.name, "Alice")

    def test_records_a_division_imported_event(self):
        import_division(self.tournament, self.owner, self._portable())
        event = self.tournament.events.get(event_type="division_imported")
        self.assertEqual(event.payload["name"], "Open")


class ImportReplayRoundTripTests(TestCase):
    def test_imported_tournament_replays_to_same_digest(self):
        owner = User.objects.create_user(username="owner", password="pw")
        _, divisions = parse_import(_bundle([{
            "name": "Open",
            "entrants": [
                {"number": 1, "player_number": "P1"},
                {"number": 2, "player_number": "P2"},
                {"number": 3, "player_number": "P3"},
            ],
            "results": [
                {"round": 1, "winner": "P1", "winner_score": 420,
                 "loser": "P2", "loser_score": 388, "winner_started": True},
                {"round": 2, "winner": "P1", "winner_score": 500,
                 "loser": "P3", "loser_score": 300, "winner_started": False},
            ],
        }]))
        div_payload = divisions[0]

        tournament = create_tournament(None, owner, {
            "name": "What-if: Nationals", "location": "sandbox",
            "start_date": "2026-07-16", "is_fake": True,
            "default_division": {"name": div_payload["name"]},
        })
        import_division(tournament, owner, div_payload)

        division = tournament.divisions.get(name="Open")
        recorded = division_digest(division)

        ctx = replay(events_from_tournament(tournament), verify=True)
        self.assertNotEqual(ctx.tournament.pk, tournament.pk)
        replayed = ctx.tournament.divisions.get(name="Open")
        self.assertEqual(division_digest(replayed), recorded)
