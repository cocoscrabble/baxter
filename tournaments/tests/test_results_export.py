import csv
from datetime import datetime, timezone
from io import StringIO

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from tournaments.models import Entrant, Player, ResultSlip
from tournaments.results_export import HEADERS, ResultRow, render_results_csv
from tournaments.tests.test_views import setUpTournament


class RenderResultsCSVTests(SimpleTestCase):
    def _rows(self, text):
        return list(csv.reader(StringIO(text)))

    def test_header_matches_ratings_format(self):
        rows = self._rows(render_results_csv([]))
        self.assertEqual(rows, [HEADERS])

    def test_row_columns_in_order(self):
        row = ResultRow(
            3, "Dave Wiegand", 541, "Alec Sjoholm", 419,
            winner_number="0233", opponent_number="0517",
        )
        _, data = self._rows(render_results_csv([row]))
        self.assertEqual(
            data[1:],
            ["3", "Dave Wiegand", "541", "Alec Sjoholm", "419", "0233", "0517"],
        )

    def test_a_row_without_numbers_leaves_those_cells_blank(self):
        """Rows assembled from a source with no numbers still render."""
        row = ResultRow(3, "Dave Wiegand", 541, "Alec Sjoholm", 419)
        _, data = self._rows(render_results_csv([row]))
        self.assertEqual(
            data[1:], ["3", "Dave Wiegand", "541", "Alec Sjoholm", "419", "", ""]
        )

    def test_submitted_on_is_excel_serial(self):
        # 1899-12-30 is Excel serial 0; noon of that day is 0.5.
        row = ResultRow(1, "A", 400, "B", 300,
                        submitted_on=datetime(1899, 12, 30, 12, 0,
                                              tzinfo=timezone.utc))
        _, data = self._rows(render_results_csv([row]))
        self.assertEqual(data[0], "0.50000")

    def test_missing_submitted_on_is_blank(self):
        _, data = self._rows(render_results_csv([ResultRow(1, "A", 400, "B", 300)]))
        self.assertEqual(data[0], "")

    def test_naive_datetime_treated_as_utc(self):
        row = ResultRow(1, "A", 400, "B", 300,
                        submitted_on=datetime(1899, 12, 31, 0, 0))
        _, data = self._rows(render_results_csv([row]))
        self.assertEqual(data[0], "1.00000")


@tag("slow")
class DivisionResultsExportViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)
        ResultSlip.objects.create(
            division=cls.division, round=1,
            winner=cls.entrant1, winner_score=450,
            loser=cls.entrant2, loser_score=380, winner_started=True,
        )

    def setUp(self):
        self.client.login(username="owner", password="testpass123")

    def _url(self):
        return reverse("division_results_export", kwargs=self.division.slug_kwargs())

    def test_downloads_csv_attachment(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("test-tournament-open-results.csv",
                      response["Content-Disposition"])

    def test_result_row_present(self):
        response = self.client.get(self._url())
        rows = list(csv.reader(StringIO(response.content.decode())))
        self.assertEqual(rows[0], HEADERS)
        self.assertEqual(
            rows[1][1:],
            [
                "1",
                "Alice", "450", "Bob", "380",
                self.player1.player_number, self.player2.player_number,
            ],
        )

    def test_same_named_players_are_told_apart_by_number_not_name(self):
        """The name column stays a plain name — it is a join key, not display
        text — and the number columns are what resolve the clash."""
        twin = Player.objects.create(
            name="Alice", player_number="0900", rating=1400
        )
        e_twin = Entrant.objects.create(
            division=self.division, player=twin, number=50
        )
        ResultSlip.objects.create(
            division=self.division, round=2,
            winner=self.entrant1, winner_score=500,
            loser=e_twin, loser_score=300, winner_started=True,
        )
        response = self.client.get(self._url())
        rows = list(csv.reader(StringIO(response.content.decode())))
        clash = next(r for r in rows[1:] if r[1] == "2")
        self.assertEqual(clash[2], "Alice")
        self.assertEqual(clash[4], "Alice")
        self.assertEqual(clash[6], self.player1.player_number)
        self.assertEqual(clash[7], "0900")
        # No "(number)" suffix leaked into the join key.
        self.assertNotIn("(", clash[2])

    def test_byes_are_omitted(self):
        bye_player = Player.get_bye()
        bye_entrant = Entrant.objects.create(
            division=self.division, player=bye_player, number=99,
        )
        ResultSlip.objects.create(
            division=self.division, round=2,
            winner=self.entrant1, winner_score=50,
            loser=bye_entrant, loser_score=0, winner_started=True,
        )
        response = self.client.get(self._url())
        rows = list(csv.reader(StringIO(response.content.decode())))
        self.assertEqual(len(rows), 2)  # header + the one real game

    def test_forbidden_for_non_editor(self):
        self.client.login(username="other", password="testpass123")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)


class RatingsReaderCompatibilityTests(SimpleTestCase):
    """The export still parses in the *actual* coco-ratings reader.

    ``coco_ratings.io.ResultCSVReader.parse_row`` unpacks the row positionally
    and swallows extras with ``*rest``, so the number columns are only safe
    where they are: appended. This test is the guard on that ordering, and it
    runs against the real reader rather than a restatement of its rules.
    """

    def _read(self, text):
        import tempfile

        from coco_ratings.core import Player
        from coco_ratings.io import ResultCSVReader

        class Roster:
            def __init__(self):
                self.players = {}

            def find_or_add_player(self, name):
                return self.players.setdefault(name, Player(name))

        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False
        ) as fh:
            fh.write(text)
            path = fh.name
        reader = ResultCSVReader(Roster(), "Test", "2026-03-15")
        reader.parse(path)
        return {
            p.name: (p.wins, p.losses, p.spread)
            for section in reader.sections
            for p in section.players
        }

    def test_the_numbered_export_reads_correctly(self):
        text = render_results_csv([
            ResultRow(1, "Alice", 450, "Bob", 380,
                      winner_number="0001", opponent_number="0002"),
            ResultRow(2, "Bob", 500, "Alice", 300,
                      winner_number="0002", opponent_number="0001"),
        ])
        self.assertEqual(
            self._read(text),
            {"Alice": (1.0, 1.0, -130), "Bob": (1.0, 1.0, 130)},
        )

    def test_interleaving_the_number_columns_would_break_it(self):
        """Why the ordering is what it is, rather than what the plan first said."""
        interleaved = (
            "Submitted On,Round,Winner,Winner Number,Winners Score,"
            "Opponent,Opponent Number,Opponents Score\n"
            ",1,Alice,0001,450,Bob,0002,380\n"
        )
        with self.assertRaises(Exception):
            self._read(interleaved)
