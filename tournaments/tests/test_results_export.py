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
        row = ResultRow(3, "Dave Wiegand", 541, "Alec Sjoholm", 419)
        _, data = self._rows(render_results_csv([row]))
        self.assertEqual(data[1:], ["3", "Dave Wiegand", "541", "Alec Sjoholm", "419"])

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
        self.assertEqual(rows[1][1:], ["1", "Alice", "450", "Bob", "380"])

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
