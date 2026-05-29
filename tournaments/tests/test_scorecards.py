from io import BytesIO

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document

from tournaments.scorecards import (
    ScorecardSpec,
    build_document,
    make_rounds,
    render_scorecards,
)
from tournaments.models import DivisionSettings, Pairing
from tournaments.tests.test_views import setUpTournament

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _spec(player_name, n_rounds=6, **kwargs):
    return ScorecardSpec(
        tournament_name="Spring Open",
        tournament_date="May 28, 2026",
        player_name=player_name,
        rounds=make_rounds(range(1, n_rounds + 1)),
        **kwargs,
    )


class MakeRoundsTests(SimpleTestCase):
    def test_first_round_is_undivided_by_default(self):
        rounds = make_rounds(range(1, 5))
        self.assertFalse(rounds[0].divided)
        self.assertTrue(all(r.divided for r in rounds[1:]))

    def test_undivided_first_can_be_disabled(self):
        rounds = make_rounds(range(1, 5), undivided_first=False)
        self.assertTrue(all(r.divided for r in rounds))


class ScorecardStructureTests(SimpleTestCase):
    def test_each_round_is_two_grid_rows(self):
        doc = build_document([_spec("Alice", n_rounds=6)])
        table = doc.tables[0]
        # header row + two rows per round
        self.assertEqual(len(table.rows), 1 + 2 * 6)

    def test_round_number_column_is_always_merged(self):
        doc = build_document([_spec("Alice", n_rounds=6)])
        table = doc.tables[0]
        # The two grid rows of round 2 (divided) share one Round-number cell.
        self.assertIs(table.cell(3, 0)._tc, table.cell(4, 0)._tc)

    def test_undivided_round_merges_all_columns(self):
        doc = build_document([_spec("Alice", n_rounds=6)])
        table = doc.tables[0]
        # Round 1 (rows 1-2) is undivided: every data column is one merged cell.
        for col in range(1, 7):
            self.assertIs(table.cell(1, col)._tc, table.cell(2, col)._tc)

    def test_divided_round_splits_only_the_last_column(self):
        doc = build_document([_spec("Alice", n_rounds=6)])
        table = doc.tables[0]
        # Round 2 (rows 3-4): columns 0-5 are merged into one cell each, and
        # only the last column (Spread) keeps two separate rows.
        for col in range(0, 6):
            self.assertIs(table.cell(3, col)._tc, table.cell(4, col)._tc)
        self.assertIsNot(table.cell(3, 6)._tc, table.cell(4, 6)._tc)

    def test_player_name_appears(self):
        doc = build_document([_spec("Carol Danvers")])
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Carol Danvers", text)

    def test_rounds_split_across_pages(self):
        # 20 rounds exceeds the 14-round first page, so it splits into two tables.
        doc = build_document([_spec("Alice", n_rounds=20)])
        self.assertEqual(len(doc.tables), 2)

    def test_one_set_of_tables_per_player(self):
        doc = build_document([_spec("Alice", n_rounds=6), _spec("Bob", n_rounds=6)])
        self.assertEqual(len(doc.tables), 2)


class OpponentPrefillTests(SimpleTestCase):
    def test_supplied_opponents_fill_their_round_only(self):
        doc = build_document(
            [_spec("Alice", n_rounds=6, opponents={1: "Bob", 3: "Carol"})]
        )
        table = doc.tables[0]
        # Opponent column is index 1; round N's row pair starts at 1 + 2*(N-1).
        self.assertEqual(table.cell(1, 1).text, "Bob")     # round 1
        self.assertEqual(table.cell(5, 1).text, "Carol")   # round 3
        self.assertEqual(table.cell(3, 1).text, "")        # round 2 left blank

    def test_no_opponents_leaves_column_blank(self):
        doc = build_document([_spec("Alice", n_rounds=6)])
        table = doc.tables[0]
        for round_idx in range(6):
            self.assertEqual(table.cell(1 + 2 * round_idx, 1).text, "")

    def test_each_player_keeps_their_own_opponents(self):
        specs = [
            _spec("Alice", opponents={1: "Bob"}),
            _spec("Bob", opponents={1: "Alice"}),
        ]
        doc = build_document(specs)
        self.assertEqual(doc.tables[0].cell(1, 1).text, "Bob")
        self.assertEqual(doc.tables[1].cell(1, 1).text, "Alice")

    def test_prefilled_division_still_shares_images(self):
        # Opponents differ per player but the clone path must still hold, so the
        # QR + logo stay a single shared pair.
        specs = [
            _spec(f"P{i}", opponents={1: f"Opp{i}"}, qr_url="https://x.test/live")
            for i in range(4)
        ]
        doc = Document(BytesIO(render_scorecards(specs)))
        self.assertEqual(len(doc.part.package.image_parts._image_parts), 2)


class RenderScorecardsTests(SimpleTestCase):
    def test_returns_openable_docx_bytes(self):
        data = render_scorecards([_spec("Alice")])
        self.assertTrue(data.startswith(b"PK"))  # docx is a zip archive
        Document(BytesIO(data))  # parses without error

    def test_qr_url_embeds_an_image(self):
        data = render_scorecards([_spec("Alice", qr_url="https://example.test/live")])
        doc = Document(BytesIO(data))
        # logo + QR = two images; without a qr_url only the logo is present.
        self.assertEqual(len(doc.inline_shapes) + _floating_image_count(doc), 2)


def _floating_image_count(doc):
    from docx.oxml.ns import qn

    return len(doc.element.findall(".//" + qn("a:blip")))


class ClonedScorecardTests(SimpleTestCase):
    """The division path clones one template per player; guard its correctness."""

    def test_every_player_gets_their_own_name(self):
        names = ["Alice Walker", "Bob Stevens", "Carol Ng"]
        specs = [_spec(n) for n in names]
        doc = Document(BytesIO(render_scorecards(specs)))
        text = "\n".join(p.text for p in doc.paragraphs)
        for name in names:
            self.assertIn(name, text)

    def test_shared_images_are_embedded_once(self):
        # QR + logo are identical across the division, so cloning should reuse
        # the same two image parts no matter how many players there are.
        specs = [_spec(f"Player {i}", qr_url="https://x.test/live") for i in range(5)]
        doc = Document(BytesIO(render_scorecards(specs)))
        self.assertEqual(len(doc.part.package.image_parts._image_parts), 2)

    def test_cloned_drawings_have_unique_ids(self):
        from docx.oxml.ns import qn

        specs = [_spec(f"Player {i}", qr_url="https://x.test/live") for i in range(4)]
        doc = Document(BytesIO(render_scorecards(specs)))
        ids = [e.get("id") for e in doc.element.iter(qn("wp:docPr"))]
        self.assertEqual(len(ids), len(set(ids)))

    def test_differing_layouts_fall_back_and_still_render(self):
        specs = [
            _spec("Alice", n_rounds=6),
            ScorecardSpec("Other Cup", "May 28, 2026", "Bob",
                          rounds=make_rounds(range(1, 9))),
        ]
        doc = Document(BytesIO(render_scorecards(specs)))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Alice", text)
        self.assertIn("Bob", text)
        self.assertIn("Other Cup", text)


@tag("slow")
class DivisionScorecardsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setUpTournament(cls)

    def test_downloads_docx_attachment(self):
        response = self.client.get(
            reverse("division_scorecards", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], DOCX_CONTENT_TYPE)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".docx", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"PK"))

    def test_filename_is_slugified(self):
        response = self.client.get(
            reverse("division_scorecards", kwargs={"pk": self.division.pk})
        )
        self.assertIn("test-tournament-open-scorecards.docx",
                      response["Content-Disposition"])

    def test_test_division_hidden_from_non_editor(self):
        self.division.is_test = True
        self.division.save()
        response = self.client.get(
            reverse("division_scorecards", kwargs={"pk": self.division.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_opponents_prefilled_from_pairings(self):
        # Keep it to 3 rounds so each player's card is a single table.
        DivisionSettings.objects.create(
            division=self.division,
            round_pairings=[{"round": 1}, {"round": 2}, {"round": 3}],
        )
        Pairing.objects.create(
            division=self.division, round=1,
            first=self.entrant1, second=self.entrant2,
        )
        response = self.client.get(
            reverse("division_scorecards", kwargs={"pk": self.division.pk})
        )
        doc = Document(BytesIO(response.content))
        # Entrants order by number: table[0] is player1's card, table[1] player2's.
        # Round 1's Opponent cell is row 1, column 1.
        self.assertEqual(doc.tables[0].cell(1, 1).text, self.player2.name)
        self.assertEqual(doc.tables[1].cell(1, 1).text, self.player1.name)
