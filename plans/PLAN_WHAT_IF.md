# Plan: "What if" scenarios

**Status: implemented** (all four phases, 2026-07-17). Design agreed 2026-07-16;
code references pinned at commit `7b0acc6`.

## Goal

Let a director re-run history: import a finished division into a sandbox
tournament they own, then ask "what if round 9 had been paired Swiss off the
round 8 standings?" and see the pairing table the engine would have produced.

Two pieces:

1. **Import** — upload a division (JSON bundle or ratings CSV) and create a
   test tournament owned by the logged-in user, with entrants, results, and
   reconstructed pairings.
2. **Explore** — a new "Explore" tab on the division pages: pick a round to
   pair, a strategy, and a based-on round; the engine pairs that one round
   hypothetically and the result renders like the "Pair rounds" table.
   Nothing is persisted.

A deliberate consequence of the design: **Explore works on any editable
division**, not just imported ones — the computation is a pure function of
existing results. Import is just the way to get historical data in; mid-
tournament "preview round 9 as KotH before changing settings" comes for free.

## Import formats (decision: support both)

The request said "the CSV format used by tournament_export.py", but
`tournament_export.py` emits a **JSON bundle** (players + ratings, entrants,
results with `winner_started`, event log); the CSV exporter is
`results_export.py` (the pdxwords/coco-ratings shape: `Submitted On, Round,
Winner, Winners Score, Opponent, Opponents Score`). Decision (2026-07-16):
**accept both** on one import page, sniffed by content (leading `{` → JSON;
otherwise expect the known CSV header).

- **JSON bundle** (`ExportTournament.from_json`): self-contained. Entrant
  numbers, ratings, and `winner_started` are all faithful. Players resolve by
  `player_number` first, then name (case-insensitive), else a provisional
  player is created (same policy as `import_entrants.resolve_players`,
  `tournaments/import_entrants.py:71`). A multi-division bundle imports all
  its divisions into the one sandbox tournament.
- **coco-ratings CSV**: matches the historical data the ratings tooling holds.
  Only names and scores are present, so:
  - **Ratings come from the Player table**: resolve each name against
    `Player` (case-insensitive); take the matched player's rating. Unknown
    names get a provisional player with rating 0 (seeded at the bottom).
  - Entrant numbers are assigned by rating, descending.
  - `winner_started` is unknown. Default it to `True` and record the
    limitation: what-if *matchings* are unaffected (starts don't feed the
    matcher), but the first/second orientation the engine assigns in explore
    output is approximate for CSV-imported divisions.

### Bye inference (both formats)

`tournament_export.py` excludes bye slips, and the ratings CSV never had
them; importing results verbatim would silently cost every byed player a
50-point win and skew the reconstructed standings. The importer therefore
infers byes: in each round that has results, any non-dropped entrant with no
result gets a materialized 50–0 bye slip against the division's bye entrant
(constants from `tournaments/generate_pairings.py:17`). Forfeits/absences are
indistinguishable from byes in these formats and will be treated as byes; the
import summary lists every inferred bye so the director can spot-check.

## Sandbox tournament shape

Follow the fake-tournament precedent (`tournaments/fake_tournament.py:30`):

- `Tournament` with `is_fake=True`, `owner=request.user`, name defaulting to
  `What-if: <source name> <timestamp>`.
- Divisions are created with `is_test=True`: hidden from non-editors, excluded
  from registry export (`tournament_export.py:113`), and it enables the
  existing simulate affordances should a later phase materialize what-ifs.
- Per division, the importer writes:
  - `Entrant` rows (numbers from the bundle, or rating order for CSV).
  - `Pairing` + `RoundPairings` rows **derived from the results**, one round
    per distinct result round, status `FINISHED`, orientation from
    `winner_started` — the same derivation `_replay_snapshot` uses
    (`tournaments/replay.py:151`). This keeps the Pairings/Results pages
    coherent instead of tripping `ERROR_NO_PAIRINGS`
    (`tournaments/pairings_view.py:222`).
  - `ResultSlip` rows linked to those pairings, plus the inferred bye slips.
  - `DivisionSettings` with a nominal schedule (`Swiss × max_round`,
    via `blocks_to_round_pairings`) so the Pair-rounds page renders sensibly.
    Every configured round is FINISHED, so `_autogenerate_pairable_rounds`
    has nothing to do; the schedule is editable afterwards like any other.

## Event log obligations

The import is a mutation, so it must be command-backed
(`test_event_completeness.py` fails otherwise — the fake-tournament view is
exempt as a dev tool, but this is a real feature and should replay).

- Tournament creation goes through the existing `create_tournament` command.
- One new command, `@records_event("division_imported")` in
  `tournaments/commands.py`, per imported division. Its payload is the
  *portable parsed division* — name-keyed entrants (name, rating, number) and
  results (winner/loser names, scores, started) — i.e. the post-parse,
  pre-DB representation, so replay does not re-parse CSV and the payload
  stays pk-free like every other event. The command performs the DB writes
  described above (bye inference included, so replay reproduces it).
- Register the command in the catalog in `tournaments/events.py`; replay then
  works through the existing `COMMAND_REGISTRY` path with no special-casing.
- The **Explore** computation persists nothing and is GET-only — no command,
  no event, and the completeness test (which walks POST views) is untouched.

## Explore: the computation

A pure function, no DB writes (new module `tournaments/whatif.py`):

```python
def explore_pairing(division, target_round, strategy, based_on, seed):
    pd = PairingData.for_division(division)
    pd.result_slips = [s for s in pd.result_slips if s.round <= based_on]
    pd.round_pairings = [RoundPairing(target_round, based_on, strategy)]
    pd.fixed_pairings = {}
    pd.published_pairings = {}
    pd.seed = seed
    return pair_with_engine(pd)   # [(target_round, [DisplayPairing, ...])]
```

Why this works:

- Truncating `result_slips` to `round <= based_on` makes the engine see the
  target round as unplayed (even when it really was played — the whole
  point), and makes `standings_after_round(k)` for any `k >= based_on` equal
  the based-on standings. Repeats are computed engine-side from the truncated
  slips, so the Repeats column reflects meetings through the based-on round.
- `based_on = 0` means pair off seedings (ratings), which
  `standings_after_round` already supports.
- Dropped entrants and the bye come along via `PairingData.for_division` —
  the engine adds a bye for an odd field exactly as it does live.
- Fixed and published pairings are cleared: explore shows the *pure* strategy
  output. (A later phase could optionally honor the round's fixed pairings.)
- Round-robin family: `normalize_round_robin_start_rounds` turns the single
  entry into a one-round block starting at `target_round`, so an RR what-if
  pairs as the first rotation off the based-on standings. Exploring a whole
  RR block is explicitly out of scope (see Future work).
- Seed: default to the division's `pairing_seed`; the UI's "reshuffle" control
  passes a different seed so `Random`/`SwissPlusRandom` what-ifs can be rerolled
  without touching stored settings.

Constraint enforced by the form: `0 <= based_on < target_round`, and
`target_round` ranges over `1 .. max_round + 1` (the `+ 1` lets a director
explore the *next* round of a live division).

## Explore: UI

- **Nav**: add `Explore` to the editor tab group in
  `tournaments/templates/tournaments/base_division.html` (after "Pair
  rounds"), `active_tab == 'explore'`.
- **Route**: `D + "explore/"` → `DivisionExploreView` (`LoginRequiredMixin`,
  `DivisionNavMixin`, `CanEditDivisionMixin`, `DetailView`), GET-only.
- **Controls** (one line, above the table): three selects and two buttons —
  *Round to pair* (default: `max_round`, i.e. re-pair the last played round),
  *Strategy* (`STRATEGY_TYPES`, default Swiss), *Based on round* (default
  `target - 1`; option 0 labelled "seedings"), a **Pair** button, and
  **Reshuffle** (visible for the random strategies). Submission is a datastar
  `@get` back to the same URL with query params; the view answers a fragment
  (`is_datastar` → `fragment_response`, same pattern as
  `DivisionStandingsView`, `tournaments/views.py:576`), so re-pairing swaps
  the table in place. Plain GET with query params renders the full page —
  results are shareable/bookmarkable as URLs, which suits a read-only tool.
- **Result header**: `Round 9 — Swiss off round 8 standings` with a
  `What-if` badge reusing the round-status badge styling.
- **Table**: same columns and markup as the Pair-rounds table
  (`_round_tab_content.html:106`): Table / First / Second / Repeats / Result.
  - Rows are lightweight view dicts (engine `DisplayPairing`s decorated in
    the view), not `Pairing` models — a separate small template that reuses
    the same CSS classes, rather than contorting `AnnotatedPairing`.
  - *Table*: board order by min standings rank of the pair, numbered 1..n —
    the same ordering rule `regenerate_pairings` uses
    (`tournaments/generate_pairings.py:260`), minus fixed-table handling.
  - *Repeats*: count from the engine; the `rd. 3, 7` detail computed from the
    truncated slips like `pair_meeting_rounds` does
    (`tournaments/pairings_view.py:155`).
  - *Result*: if the hypothetical pairing **actually happened** in the target
    round, show the real score — instant "same as reality, and this is how it
    went" feedback. Empty otherwise.
- **Comparison panel** (phase 4): alongside the what-if table, show the actual
  pairings of the target round (when the round was really played) in a second
  table, reusing the `pairings-body--split` layout; rows common to both sides
  get a ✓. This is the "what changed" view that makes the feature sing.

## Import: UI

- Top-level page beside the fake-tournament tool: route `what-if/import/`
  (before the tournament-slug catch-all in `tournaments/urls.py:57`), linked
  from the tournament list next to "Create test tournament".
- Form: paste-or-upload (textarea + file input, file wins), tournament name
  (pre-filled default). POST parses, creates the sandbox via the commands
  above, shows an import summary (players matched / created provisional,
  results per round, inferred byes), and redirects to the first division's
  **Explore** tab.
- Parse errors re-render the form with messages; nothing is created on error
  (single transaction).

## Phases

### Phase 1 — importer core + command

`tournaments/whatif_import.py`: JSON/CSV sniffing, both parsers producing one
portable division dict; player resolution (number → name → create
provisional); bye inference. `division_imported` command in `commands.py` +
catalog entry in `events.py`. **Verify**: unit tests for both parsers (happy
path, malformed input, unknown players, bye inference); a replay round-trip
test — import, `replay_tournament --verify` style digest comparison — passes.

### Phase 2 — import view

Form, template, URL, nav link; command-backed wiring; import summary page.
**Verify**: view tests (auth required, both formats end-to-end, error paths);
`test_event_completeness` green with the new view command-backed;
`uv run python manage.py test tournaments.tests`.

### Phase 3 — Explore tab

`tournaments/whatif.py` (`explore_pairing` + row decoration), `DivisionExploreView`,
template + nav tab, datastar fragment swap. **Verify**: view tests — pd
truncation (a slip in round 9 must not influence a round-9 what-if off round
8), `based_on=0` seedings path, form validation bounds, odd field gets a bye
row, actual-result decoration; manual pass on an imported division and on a
live test division.

### Phase 4 — comparison polish

Side-by-side actual-round table with common-row ✓; reshuffle button for
random strategies. **Verify**: view test that common pairs are flagged;
manual UI pass.

## Future work (explicitly out of scope)

- **Adopt this pairing**: materialize a what-if into the sandbox division
  (unpublish/delete later rounds, write the pairing, simulate forward with
  the existing simulate tooling). Powerful, but it is a destructive mutation
  with real lifecycle questions — separate plan if wanted.
- **Multi-round what-ifs**: explore a whole RR/quads block or a sequence of
  rounds (requires simulated results between rounds).
- **Honoring fixed pairings** in explore, as an on/off toggle.
- **CSV starts inference**: recover who-went-first heuristics for ratings
  CSVs if orientation accuracy ever matters.
