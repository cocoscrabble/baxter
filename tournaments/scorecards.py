"""Generate printable player scorecards as a Word (.docx) document.

This module is deliberately free of Django imports so it can be exercised in
isolation (tests, management commands, future async jobs). Callers build a list
of :class:`ScorecardSpec` — one per player — and hand it to
:func:`render_scorecards`, which returns the ``.docx`` file as bytes.

Layout per player: a centred title block (tournament name / date / player name)
with the COCO logo floated left and a QR code (encoding the live-coverage URL)
floated right, followed by one or more bordered round tables and a footer. Each
round is two grid rows so the player can record two games; the Round-number
cell is vertically merged so the label shows once. A round marked ``divided=
False`` (round 1) merges every column instead, leaving a single open block with
no internal divider.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from io import BytesIO

import qrcode
from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.table import _Cell
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsmap, qn
from docx.shared import Emu, Pt

FONT = "Source Sans Pro"
HEADER_FILL = "f2f2f2"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "coco_logo.png")

# Column widths in twentieths of a point (dxa).
COL_WIDTHS = [1081, 2604, 900, 900, 1620, 1710, 1530]
HEADERS = ["Round", "Opponent", "Won", "Lost",
           "Player Score", "Opponent Score", "Spread"]
ROW_HEIGHT = 346  # dxa; every grid row uses this so the table stays uniform.

# Image geometry (EMU), reproduced from the reference scorecard.
LOGO_SIZE = 840658
QR_SIZE = 840105
LOGO_H_OFFSET, LOGO_V_OFFSET = 1, -634
QR_H_OFFSET, QR_V_OFFSET = 5789295, 7315


@dataclass(frozen=True)
class RoundSpec:
    """One round on the scorecard.

    ``divided`` rounds get two writable lines (a horizontal divider between
    them); undivided rounds present a single open block of the same height.
    """

    number: int
    divided: bool = True


@dataclass(frozen=True)
class ScorecardSpec:
    """Everything needed to render one player's scorecard."""

    tournament_name: str
    tournament_date: str
    player_name: str
    rounds: list[RoundSpec]
    # Optional {round number: opponent name} to prefill the Opponent column.
    opponents: dict[int, str] = field(default_factory=dict)
    qr_url: str = ""
    footer_text: str = "Submit results and view pairings and standings at:"
    footer_url: str = "cocoscrabble.org/live-coverage"
    # Rounds before the first page break, and per continuation page thereafter.
    first_page_rounds: int = 14
    rounds_per_page: int = 20


def make_rounds(round_numbers, *, undivided_first=True):
    """Build a RoundSpec list from round numbers, optionally leaving round 1 open."""
    specs = []
    for i, n in enumerate(round_numbers):
        specs.append(RoundSpec(number=n, divided=not (undivided_first and i == 0)))
    return specs


# --- low-level docx helpers -------------------------------------------------

def _set_run_font(run, size, *, bold=False):
    run.font.size = Pt(size)
    if bold:
        run.font.bold = True
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT)


def _style_paragraph(para):
    pf = para.paragraph_format
    pf.space_after = Pt(0)
    pf.line_spacing = 1.0


def _add_paragraph(doc, text, size, *, bold=False, center=True):
    p = doc.add_paragraph()
    _style_paragraph(p)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if text:
        _set_run_font(p.add_run(text), size, bold=bold)
    return p


def _set_cell_shading(cell, fill):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _set_cell_width(cell, dxa):
    tcPr = cell._tc.get_or_add_tcPr()
    tcW = tcPr.find(qn("w:tcW"))
    if tcW is None:
        tcW = OxmlElement("w:tcW")
        tcPr.append(tcW)
    tcW.set(qn("w:w"), str(dxa))
    tcW.set(qn("w:type"), "dxa")


def _set_grid_widths(table, widths):
    for col, width in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths):
        col.set(qn("w:w"), str(width))


def _set_table_borders(table):
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "000000")
        borders.append(el)
    table._tbl.tblPr.append(borders)


def _set_fixed_layout(table):
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    table._tbl.tblPr.append(layout)


def _set_row_height(row, dxa):
    trPr = row._tr.get_or_add_trPr()
    h = OxmlElement("w:trHeight")
    h.set(qn("w:val"), str(dxa))
    h.set(qn("w:hRule"), "atLeast")
    trPr.append(h)


def _clear_cell(cell):
    for p in list(cell.paragraphs):
        p._element.getparent().remove(p._element)


def _center_cell(cell):
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _style_paragraph(cell.paragraphs[0])
    cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER


def _fill_header_cell(cell, text):
    _set_cell_shading(cell, HEADER_FILL)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    p = cell.paragraphs[0]
    _style_paragraph(p)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p.add_run(text), 10, bold=True)


def _fill_round_cell(cell, round_number):
    """Round number on the first line, '1st   2nd' (superscript) below it."""
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _clear_cell(cell)

    p1 = cell.add_paragraph()
    _style_paragraph(p1)
    p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_run_font(p1.add_run(str(round_number)), 10)

    p2 = cell.add_paragraph()
    _style_paragraph(p2)
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for text, sup in [("1", False), ("st", True), ("   2", False),
                      ("nd", True), (" ", False)]:
        run = p2.add_run(text)
        _set_run_font(run, 10)
        if sup:
            run.font.superscript = True


_uid = [1000]


def _add_floating_image(paragraph, image, cx, cy, h_offset, v_offset, *, behind):
    """Anchor a floating image (path or stream) at an absolute offset in EMU."""
    run = paragraph.add_run()
    run.add_picture(image, width=Emu(cx), height=Emu(cy))
    inline = run._r.find(qn("w:drawing"))[0]
    graphic = inline.find(qn("a:graphic"))

    _uid[0] += 1
    anchor = parse_xml(
        f'<wp:anchor xmlns:wp="{nsmap["wp"]}" xmlns:a="{nsmap["a"]}" '
        f'xmlns:r="{nsmap["r"]}" xmlns:pic="{nsmap["pic"]}" '
        f'simplePos="0" relativeHeight="0" behindDoc="{1 if behind else 0}" '
        'locked="0" layoutInCell="1" allowOverlap="1" '
        'distT="0" distB="0" distL="0" distR="0">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="column"><wp:posOffset>{h_offset}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="paragraph"><wp:posOffset>{v_offset}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:wrapNone/>'
        f'<wp:docPr id="{_uid[0]}" name="image{_uid[0]}"/>'
        '<wp:cNvGraphicFramePr/>'
        '</wp:anchor>'
    )
    anchor.append(graphic)
    drawing = run._r.find(qn("w:drawing"))
    drawing.remove(inline)
    drawing.append(anchor)


def _make_qr_png(data):
    """Render ``data`` as a QR code and return PNG bytes."""
    img = qrcode.make(data)  # PilImage; its default kind is PNG
    buf = BytesIO()
    img.save(buf)
    return buf.getvalue()


# --- assembly ---------------------------------------------------------------

def _row_cells(row, table):
    """The row's cells straight off its ``<w:tc>`` children.

    ``_Row.cells`` / ``Table.cell`` rebuild the whole table grid on every
    access (O(rows*cols) each), which makes filling a large table quadratic.
    Reading ``tr.tc_lst`` is O(1) and there are no horizontal merges here, so
    each row's ``<w:tc>`` list is exactly its column cells.
    """
    return [_Cell(tc, table) for tc in row._tr.tc_lst]


def _set_vmerge(cell, *, restart):
    """Vertically merge this cell with the one below (restart) or into it (continue)."""
    vMerge = cell._tc.get_or_add_tcPr().get_or_add_vMerge()
    vMerge.val = "restart" if restart else None


def _add_round_table(doc, round_specs, opponents):
    """One bordered table covering ``round_specs`` (each round = two grid rows).

    ``opponents`` is a {round number: name} mapping used to prefill the
    Opponent column; rounds absent from it are left blank.
    """
    table = doc.add_table(rows=1 + 2 * len(round_specs), cols=len(HEADERS))
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _set_grid_widths(table, COL_WIDTHS)
    _set_fixed_layout(table)
    _set_table_borders(table)

    rows = table.rows
    _set_row_height(rows[0], ROW_HEIGHT)
    for cell, text, width in zip(_row_cells(rows[0], table), HEADERS, COL_WIDTHS):
        _set_cell_width(cell, width)
        _fill_header_cell(cell, text)

    r = 1
    for spec in round_specs:
        top = _row_cells(rows[r], table)
        bottom = _row_cells(rows[r + 1], table)
        _set_row_height(rows[r], ROW_HEIGHT)
        _set_row_height(rows[r + 1], ROW_HEIGHT)
        for width, top_cell, bottom_cell in zip(COL_WIDTHS, top, bottom):
            _set_cell_width(top_cell, width)
            _set_cell_width(bottom_cell, width)

        # Every column is vertically merged across the round's two rows except
        # the last (Spread), which stays split into two rows for divided rounds
        # (round 1 is undivided, so even Spread merges into one open block).
        last = len(HEADERS) - 1
        for c in range(len(HEADERS)):
            split = c == last and spec.divided
            if not split:
                _set_vmerge(top[c], restart=True)
                _set_vmerge(bottom[c], restart=False)
            _center_cell(top[c])
            _center_cell(bottom[c])

        # Round number goes in the (merged) first cell so it appears once.
        _fill_round_cell(top[0], spec.number)

        # Prefill the (merged) Opponent cell if a name was supplied.
        opponent = opponents.get(spec.number, "")
        if opponent:
            _set_run_font(top[1].paragraphs[0].add_run(opponent), 10)
        r += 2

    return table


def _add_page_break(doc):
    p = doc.add_paragraph()
    p.add_run().add_break(WD_BREAK.PAGE)


def _chunk_rounds(spec):
    """Split rounds into per-page groups (smaller first page leaves room for the title)."""
    rounds = spec.rounds
    chunks = [rounds[: spec.first_page_rounds]]
    rest = rounds[spec.first_page_rounds:]
    for i in range(0, len(rest), spec.rounds_per_page):
        chunks.append(rest[i: i + spec.rounds_per_page])
    return [c for c in chunks if c]


def build_scorecard(doc, spec):
    """Append one player's scorecard to ``doc`` (assumes the page is fresh)."""
    title = _add_paragraph(doc, spec.tournament_name, 25, bold=True)
    if spec.qr_url:
        _add_floating_image(title, BytesIO(_make_qr_png(spec.qr_url)),
                            QR_SIZE, QR_SIZE, QR_H_OFFSET, QR_V_OFFSET, behind=True)
    _add_floating_image(title, LOGO_PATH, LOGO_SIZE, LOGO_SIZE,
                        LOGO_H_OFFSET, LOGO_V_OFFSET, behind=False)

    _add_paragraph(doc, spec.tournament_date, 20)
    for _ in range(3):
        _add_paragraph(doc, "", 11)
    _add_paragraph(doc, spec.player_name, 30, bold=True, center=False)
    _add_paragraph(doc, "", 11, center=False)

    chunks = _chunk_rounds(spec)
    for i, chunk in enumerate(chunks):
        _add_round_table(doc, chunk, spec.opponents)
        if i < len(chunks) - 1:
            _add_page_break(doc)

    _add_paragraph(doc, "", 11)
    _add_paragraph(doc, "", 11)
    footer = _add_paragraph(doc, "", 14)
    _set_run_font(footer.add_run(spec.footer_text + "\n"), 14)
    _set_run_font(footer.add_run(spec.footer_url), 14, bold=True)
    _add_paragraph(doc, "", 11)


def _configure_page(section):
    section.page_width = Emu(7772400)    # 8.5"
    section.page_height = Emu(10058400)  # 11"
    section.left_margin = Emu(594360)
    section.right_margin = Emu(548640)
    section.top_margin = Emu(548640)
    section.bottom_margin = Emu(274320)


# Placeholder dropped into the template's player-name run, then swapped per clone.
_NAME_SENTINEL = "PLAYER_NAME"


def _opp_sentinel(round_number):
    return f"OPPONENT_{round_number}"


def _layout_signature(spec):
    """What fixes a scorecard's structure: everything but the per-player values
    (player name and opponent names) that get patched into clones."""
    return replace(spec, player_name="", opponents={})


def _patch_text(element, replacements):
    """Replace any ``<w:t>`` whose text is a key of ``replacements`` in place."""
    for t in element.iter(qn("w:t")):
        if t.text in replacements:
            t.text = replacements[t.text]


def _reassign_drawing_ids(element):
    """Give cloned drawings fresh ids so Word doesn't see duplicate object ids."""
    for docPr in element.iter(qn("wp:docPr")):
        _uid[0] += 1
        docPr.set("id", str(_uid[0]))
        docPr.set("name", f"image{_uid[0]}")


def build_document(specs):
    """Build a Document with one scorecard per spec, each starting a new page.

    Within a division every scorecard shares the same structure, QR and logo;
    only the player name and any prefilled opponent names differ. So we build
    the first one as a template (with placeholders in those spots) and deep-copy
    its XML for the rest, patching only the placeholders. That skips almost all
    per-element construction and renders the QR / reads the logo once. Specs
    whose layout differs from the first fall back to a fresh build.
    """
    doc = Document()
    _configure_page(doc.sections[0])
    body = doc.element.body
    for p in list(doc.paragraphs):
        body.remove(p._element)
    sectPr = body.find(qn("w:sectPr"))

    if not specs:
        return doc

    # Rounds that any player has a prefilled opponent for get a placeholder in
    # the template so every clone can patch its own name into that slot.
    prefill_rounds = {n for spec in specs for n in spec.opponents}
    template_opponents = {n: _opp_sentinel(n) for n in prefill_rounds}
    build_scorecard(doc, replace(
        specs[0], player_name=_NAME_SENTINEL, opponents=template_opponents
    ))
    template_els = [c for c in body if c is not sectPr]
    for el in template_els:
        body.remove(el)

    def append(el):
        sectPr.addprevious(el) if sectPr is not None else body.append(el)

    template_sig = _layout_signature(specs[0])
    for i, spec in enumerate(specs):
        if i > 0:
            _add_page_break(doc)
        if _layout_signature(spec) == template_sig:
            replacements = {_NAME_SENTINEL: spec.player_name}
            for n in prefill_rounds:
                replacements[_opp_sentinel(n)] = spec.opponents.get(n, "")
            for el in template_els:
                clone = copy.deepcopy(el)
                _reassign_drawing_ids(clone)
                _patch_text(clone, replacements)
                append(clone)
        else:
            build_scorecard(doc, spec)
    return doc


def render_scorecards(specs):
    """Render the scorecards for ``specs`` and return the .docx file as bytes."""
    buf = BytesIO()
    build_document(specs).save(buf)
    return buf.getvalue()
