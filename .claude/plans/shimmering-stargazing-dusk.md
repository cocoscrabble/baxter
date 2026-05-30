# Baxter UI revamp — laptop-first layout + refined refresh

## Context

Baxter is used mainly on laptops, but `static/css/style.css` traps the navbar, flash
messages, **and** all page content inside one 800px centred column
(`--max-width: 800px; body { max-width: 800px; margin: 0 auto }`). On a 1280–1536px
laptop ~40% of the width is empty, while every page is a single vertical stack
(`<h1>` → one table), so narrow tables (e.g. Standings — 6 skinny numeric columns) are
squished and long lists scroll far off-screen despite all that unused horizontal room.
Generous vertical metrics (`line-height: 1.6`, 10px cells, 20px table margins, a
non-sticky 11-tab nav that wraps to two rows) make the overflow worse.

Goal: use the screen's real width, cut vertical overflow, and give the app a more
polished, cohesive look — without changing behaviour.

Direction agreed with the user:
- **Layout + visual refresh** (not a bold theme): keep it recognizably Baxter, purple
  kept as accent, calmer surfaces, distinctive heading font + tabular-figure data font.
- **Full-width sticky top bar** (keep tabs on top; the horizontal win comes from the
  widened content region below).
- **Compact density**: tighter rows, tabular numbers, more rows visible per screen.

## Hard constraint — preserve Datastar swap targets

Several endpoints live-swap elements **by id**; layout changes must keep these ids on
the swapped nodes and keep the morph target present in the page:
`#pairings-body`, `#pairings-area`, `#round-tab-content`, `#pairings-controls`
(`tournaments/views.py` `_pairings_body_response`, `RoundPairingsTabView`),
`#standings-content` (`_standings_content.html`, `DivisionStandingsView`),
`#resultslip-form-container` (`_resultslip_form.html`). New grid wrappers go *inside*
or *around* these ids without removing them. Tests in `tournaments/tests/test_views.py`
also assert visible strings (e.g. "Publish round 1", "Add fixed pairing") and
`selected_status` — keep that text and context intact.

## Phase 1 — Foundation (the bulk of the win; mostly CSS + base.html)

### App shell (kills the 800px cap)
- `templates/base.html`: wrap the nav in `<header class="app-header"><div class="nav-inner">…</div></header>`,
  and wrap messages + `{% block content %}` in `<main class="app-main"><div class="page">…</div></main>`.
- `static/css/style.css`:
  - `body` → full width (`margin:0; padding:0`), page background = new `--surface`.
  - New tokens: `--content-max: 1440px`, `--pad-inline: clamp(16px, 4vw, 40px)`.
  - `.nav-inner` and `.page` both: `max-width: var(--content-max); margin-inline: auto;
    padding-inline: var(--pad-inline)`. Header background is full-bleed; its inner
    content aligns to the same container as the page (so nav and content share gutters).
  - `.app-header { position: sticky; top: 0; z-index: 50 }` with a bottom border +
    subtle shadow → nav stays put while tables scroll (direct fix for vertical overflow).

### Typography (self-hosted, offline-safe for venues)
- Add `@font-face` rules + woff2 files under `static/fonts/` and a fallback stack;
  load via a small `static/css/fonts.css` linked in `base.html` `<head>`.
  Recommended (swappable at approval):
  - Display/headings: **Bricolage Grotesque** — characterful, not an overused default.
  - UI + data: **Hanken Grotesk** with `font-variant-numeric: tabular-nums`.
  - Optional scoreboard accent: **JetBrains Mono** for score/number cells only.
- Set `--font-display` / `--font-body` / `--font-mono` tokens; apply display to
  `h1–h3`, body elsewhere. (Quick alternative if self-hosting is deferred: Google Fonts
  `<link>` with the same families + system fallback.)

### Compact tables + sticky headers (global)
- `th,td`: padding `6px 10px`; `td { font-variant-numeric: tabular-nums }`; zebra
  striping + row hover for scanability; `--line-height: 1.45`.
- `thead th { position: sticky; top: var(--header-h) }` so headers stay visible on long
  tables. Define `--header-h` to the sticky header height.
- Numeric columns right-aligned via a `.num` class added to the relevant `<th>/<td>`
  in templates (scores, wins/losses/ties/spread, ratings, table #).

### Bounded table viewport — controls stay on screen
Long tables (edit results / entrants, all results, standings) must not push their action
buttons off the bottom of the page. Pattern: the page-level controls live in a sticky
toolbar at the top; the table body lives in a height-bounded region that scrolls
internally, so the page itself never grows past the viewport.

- **Tabulator edit pages** (the five `*_edit` views): add a bounded
  `maxHeight: "calc(100vh - var(--header-h) - <toolbar+gutter>)"` to the shared
  `TABLE_DEFAULTS` in `tournaments/static/tournaments/js/table_helpers.js` — one change
  gives every grid an internally-scrolling body with Tabulator's own frozen header row.
  Then move the existing **Add Row / Save / status** toolbar (currently the `<p>` *below*
  the `#…-table` div in `division_edit_results.html`, `division_entrants_edit.html`,
  `division_fixed_pairings_edit.html`, `division_fixed_tables_edit.html`,
  `division_board_table_map_edit.html`) into a sticky `.table-toolbar` *above* the grid
  (in the `.page-head`), so Add Row / Save are always reachable while rows scroll.
- **Plain long tables** (`_standings_content.html`, `division_all_results.html`): wrap the
  `<table>` in `.table-scroll { max-height: calc(100vh - var(--header-h) - …);
  overflow:auto }` with the sticky `thead` from the density work; keep page actions in the
  sticky `.page-head` rather than below the table.

### Surfaces, buttons, page-head pattern
- Add surface tokens (`--surface` warm off-white page, `--surface-raised` white cards,
  softened `--border`); keep `--color-primary` purple as the accent.
- Compact buttons (`padding: 8px 14px`); keep primary purple, refine secondary/cancel.
- New `.page-head` (flex row: `<h1>` left, page actions right) so per-page controls sit
  beside the title instead of stacking. Apply to the content templates' top `<h1>`.

## Phase 2 — Per-page multi-column (uses the freed horizontal space)

Add lightweight grid utilities in CSS (`.split-2`, `.side-rail`, `.table-narrow`,
`.card`, `.card-grid`) and wrap content per page:

- **Standings** (`division_standings.html` / `_standings_content.html`): `.page-head`
  with the round selector on the right; cap the table (`.table-narrow`, ~640px) and put
  it in a 2-col layout beside a small side panel (round list / after-round summary), so
  it stops stretching and the controls leave the vertical stack. Keep `#standings-content` id.
- **Pairings** (`_round_tab_content.html`, `_pairings_controls.html`): for a pairable
  round, place the pairings table and the **fixed-pairings editor side by side**
  (`.split-2`) instead of stacked — big vertical saving — with Publish actions in the
  `.page-head`. Keep `#pairings-body/#pairings-area/#round-tab-content` ids on the
  swapped nodes.
- **Results / All results / Entrants** (`division_all_results.html`,
  `division_detail.html`, `division_entrants.html`): `.page-head`, capped + `.num`
  columns, sticky headers; long results render 2-up via a responsive column grid.
- **Tournament detail** (`tournament_detail.html`): two columns — metadata `.card` on
  the left, divisions as a responsive `.card-grid`
  (`repeat(auto-fill, minmax(220px,1fr))`) on the right.
- **Add Result** (`resultslip_form.html` / `_resultslip_form.html`): **stays a single
  vertical stack** — this form is also used on mobile, so no two-pane / side panel.
  Apply only the shared typography + density + compact-button styling; keep
  `#resultslip-form-container` id. Cap the form column width (e.g. ~480px) so it doesn't
  stretch awkwardly on a wide laptop.
- **Tabulator edit pages**: structural change is the bounded-viewport pattern above
  (sticky toolbar + `maxHeight` scroll). They otherwise inherit the wider `.page`
  container; verify the grids fill it and the header row freezes on scroll.

## Critical files
- `static/css/style.css` (most of the work — layout/tokens/tables/components,
  `.table-scroll`, `.table-toolbar`, `.page-head`, grid utilities), new
  `static/css/fonts.css`, `static/fonts/*.woff2`.
- `templates/base.html` (app shell + font link), `tournaments/templates/tournaments/base_division.html`
  (tab-bar classes).
- `tournaments/static/tournaments/js/table_helpers.js` (`TABLE_DEFAULTS` bounded `maxHeight`).
- The five `*_edit.html` templates (move toolbar above grid into sticky `.table-toolbar`).
- Content templates listed in Phase 2 (page-head + grid wrappers); Add Result stays a
  capped vertical stack.

## Verification
- `uv run python manage.py runserver`; with chrome-devtools MCP at 1440×900, screenshot
  Tournament detail, Standings, Pairings (pairable round), All results before/after —
  confirm content fills width, nav + table headers stay sticky, narrow tables are capped
  (not stretched), and no horizontal scrollbar. Spot-check ~1280 and a narrow ~900 width.
- Long-table check: open **Edit Results** with enough rows to exceed the viewport —
  confirm the rows scroll *inside* the grid while Add Row / Save stay pinned and visible,
  and the page itself doesn't grow a second scrollbar. Repeat for All Results / Standings.
- Mobile check: narrow the viewport (~390px) on **Add Result** — confirm it remains a
  usable single-column stack.
- Exercise Datastar flows by hand: switch round tabs, switch standings round, add/remove
  a fixed pairing, publish a round — confirm the live swaps still patch correctly (ids intact).
- `uv run python manage.py test tournaments.tests` — should stay green (changes are
  structural/visual; asserted strings and ids preserved).

## Suggested execution order
Phase 1 first (foundation gives the headline horizontal/vertical win with low risk and
few template edits); get a look, then Phase 2 per-page layouts.
