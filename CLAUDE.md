# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Baxter is a scrabble tournament manager built with Django 6.0 and Python 3.14.

Design/implementation plans live in `plans/` (see `plans/README.md`) — put new
plans there, not in the repo root.

## Development Commands

```bash
npm install        # JS deps (Tabulator for the edit grids) — served from
                   # node_modules via django-node-assets. Without it, dev grids
                   # 404 Tabulator and render blank (prod bakes it in via Dockerfile).

# Rebuild the Rust pairing extension after editing scrabble-pairing*/ crates
# (uv does not watch the crate source)
make rust-engine   # == uv sync --reinstall-package scrabble-pairing-py
```

## Pairing engine (Rust)

The pairing computation is the `scrabble-pairing` Rust crate, called from Python
via the `scrabble-pairing-py` PyO3 extension (a separate crate so the core stays
wasm-clean). `scrabble_pairing_py.pair_json(str) -> str` is the boundary;
`tournaments/pairing/engine.py::pair_with_engine` is the single Python entry
point. Building requires a local Rust toolchain; `uv sync` builds the wheel via
maturin (`make rust-engine` to rebuild after crate edits).

**The Python engine has been deleted** — engine changes are Rust-only now (edit
`scrabble-pairing/src/`, add a `cargo test`). What stays in Python is the
ORM-facing layer: `PairingData` assembly, `standings_after_round`/`seedings`
(standings display), `Repeats`/`Starts`, `PairingError`, and the publish/
regenerate lifecycle. `tests/corpus/cases.json` is a **frozen** regression
fixture (the Python oracle that generated it is gone); `cargo test` still checks
the Rust engine against it.

## Playoffs

A division may carry a `Playoff`: a 2/4/8-player bracket with per-series
best-of-N lengths, run either after the main schedule (postscript) or alongside
it (concurrent). See `plans/PLAN_PLAYOFFS.md`.

The bracket is **derived, never stored**. The only recorded intent is the
playoff's configuration plus its confirmed seed snapshot (`playoff_created`);
who meets whom, series scores, which games still need playing, and final
placements are all computed by `tournaments/playoff.py` from that plus the
division's results. Consequences worth knowing before changing anything here:

- A game is only ever generated when it is *certainly* necessary, so a game a
  clinch made pointless never exists — no bye, no zero score, no export row.
- `regenerate_pairings` builds playoff games alongside engine pairings; a
  published window is repaired in both directions (a decider that turns out to
  be needed is added, a retired game is deleted).
- `PlayoffSeries` rows are derived structure only (participants, length, window),
  upserted like `RoundPairings`; never store a score or winner on them.
- Concurrent mode reserves bracket players from ordinary pairing through the
  engine's generic `inactive_players` (round -> names): no game, no bye, and not
  withdrawn. `Entrant.dropped` is never touched by a playoff.

## Starts

A published pairing is a printed board, and it **owns the start**. Its
first/second assignment is authoritative from the moment the round is published
— before any result is entered — and a result slip keyed the other way round is
rewritten to match, as a logged `result_starts_corrected` event.

The rule is stated in two places that must agree: `tournaments/starts.py`
(`PUBLISHED_PAIRING_OWNS_THE_START`, which drives the rewrite) and
`scrabble-pairing/src/pair.rs` (`PUBLISHED_ORIENTATION_WINS`, which drives the
engine's ledger). Both are single constants so the policy can be reversed.

The board owns the *start*, never *who played whom*: a saved pairing whose
players turn up in some other game that round is stale and dropped. Bye rows are
excluded throughout — they are stored real-player-first for display, the
opposite of the ledger's convention that the bye opponent is the notional
starter (`PairingData.for_division` flips them back).

## Event log

Every state-changing action is recorded as an append-only `TournamentEvent`
(see PLAN_EVENT_LOG.md). Mutations go through **commands** (`@records_event` in
`tournaments/commands.py` + domain modules; grid saves via the editgrid
`on_saved` hook) — not direct view writes. Payloads are pk-free (name-keyed) so
the log replays into a fresh DB. Key pieces:

- `tournaments/events.py` — recorder, `division_digest` (excludes DRAFT rounds),
  the command catalog, the opt-in `strict_write_guard`.
- `tournaments/replay.py` + `replay_tournament` command — reconstruct a
  tournament from its log (`--verify` compares digests).
- `tournaments/fuzz.py` + `fuzz_tournament` command + `test_fuzz` — seeded
  fuzzer whose meta-invariant is that replay reproduces the digest.
- Activity page + `export_event_log` (JSONL); `snapshot_tournaments` backfills
  pre-log tournaments.

**A new mutating POST view must route through a command** (or be added to the
exempt set in `test_event_completeness.py`, which fails CI otherwise).

## Admin pages

Anything gated on the Admin role is listed at `/tournaments/manage/`
(`AdminIndexView`), and admins get an **Admin** link in the navbar — the only
navigation into it. The three player pages (roster pull, player import, WESPA
import) used to link only to each other, so they were reachable only by typing a
URL; that stopped being tolerable when the roster pull started running on a
timer and could leave work waiting on a page nobody visited.

**A new admin-only view must appear on that page.**
`test_admin_index.CompletenessTests` reads the URLconf for every view gated on
`IsAdminMixin` and fails if one is not linked there, so a new page cannot
quietly become unreachable the way those three were.

The page shows the roster's *state*, not just links: a guest awaiting
confirmation, or a scheduled pull that has been failing, is flagged here.
`/manage/` mirrors the sibling cocodb site's staff area; `/admin/` stays Django's
own, which is gated on `is_staff` rather than on the role.

## Roster sync (the central player database)

Baxter mirrors player identity and CoCo ratings from the central database
(`cocodb`, the `../ratings` repo). `tournaments/roster_import.py` fetches and
upserts a `coco.roster/1` document; `tournaments/roster_sync.py` runs that and
records the outcome. See `plans/PLAN_COCO_PROGRAM.md` (W4).

**It runs unattended.** `app.json` declares a Dokku cron entry that runs
`manage.py pull_roster` every six hours, so the player table keeps up on its own;
it is `uv run --no-sync` because a plain `uv run` rebuilds the Rust extension
inside every one-off container (the Dockerfile's `COPY . .` lands fresh mtimes on
the crate sources after `uv sync`);
`/players/roster/` is for pulling sooner than that, or for uploading a snapshot
at an event with no connection. All three paths go through `run_sync`, so they
leave the same kind of record.

Two things follow from nobody watching a scheduled pull, and both are the reason
`RosterSync` exists at all:

- **A failure has to be visible.** A rotated `ROSTER_API_TOKEN` would otherwise
  401 four times a day in silence (and `../vps` sets Dokku config with
  `--no-restart`, so a rotation lands on the next deploy, not immediately). The
  command exits non-zero for cron, and every attempt writes a `RosterSync` row
  that `/players/roster/` shows.
- **Held-back rows have to outlive the run.** A pull that finds a roster number
  whose name matches exactly one local guest changes nothing and offers the
  match for confirmation — matching by name is a guess, so it is the one step a
  human makes. Those live on the record, not in the puller's session, which is
  what lets a director confirm what a cron tick found.

A pull cannot disturb a running event: entrants freeze their whole rating seed
at registration (`plans/PLAN_ENTRANTS.md` decision 3), which is what makes an
unattended pull safe at any hour.

## WESPA ratings (the other rating list)

Baxter keeps a **local mirror of the whole WESPA rating list** — `WespaPlayer`,
some 9,200 rows — pulled from one bulk JSON document
(`WESPA_API_URL`, default `wespa-api.xerafin.net/players.php?idsonly=1`) by
`tournaments/wespa_api.py` + `wespa_ratings.py`, run and recorded by
`wespa_sync.run_sync` and `app.json`'s weekly `pull_wespa` cron entry. See
`plans/PLAN_WESPA.md`.

**Why the whole list and not just the ratings.** The players Baxter has never
seen are the point: an overseas visitor has no CoCo number and no CoCo rating, so
the player table can never find them, and their rating used to be typed in at the
registration desk from a website. The registration page searches the mirror
alongside the player table, and entering a WESPA-only hit mints the guest with
their name, rating and `wespa_id` already filled in. Everything else in this
section exists to make that search trustworthy.

Four rules, all easy to break:

- **A pull creates no `Player` and deletes nothing.** A WESPA row becomes a
  player when a director enters one, not before.
- **`Player.wespa_id` is the link, and links survive renames.** It is set when a
  human picks the row, or when a name is unique on *both* sides — the same guess
  the old CSV import made, now visible and undoable. Everything else waits.
- **Ambiguity is held back and listed; absence is not.** A name shared by several
  people links nobody and lands on `WespaSync.pending`. A player with no WESPA
  row is the normal case and is not reported — a pending list holding most of the
  roster is a list nobody reads. Spelling mismatches are linked by hand at
  `/players/wespa/`.
- **Still unlogged and still global**, for the reason below: entrants pin their
  rating at entry, so a weekly pull cannot move a live event. The one logged
  part is `player_created`, which now carries `wespa_id` so a replay recreates a
  guest already linked.

## Entrant ratings

An entrant freezes their whole rating seed at registration, and the roster pull
cannot move it — that is what makes the six-hourly sync safe mid-tournament
(`plans/PLAN_ENTRANTS.md` decision 3).

The exception is deliberate and per-entrant: `/entrants/refresh-ratings/`
re-pins the ticked entrants from the player table. The entrants page shows the
drift for editors, with checkboxes, and warns if a round has already left draft.

Three rules it follows, all easy to break:

- **Manual ratings are never offered.** A typed rating is a director saying what
  a player is worth; a sync does not overrule it.
- **The event records the values it wrote**, not the intent to sync. Entrant
  ratings are in `division_digest`, so an event meaning "take whatever the
  roster says" would replay to a different digest every time.
- **The drift column is editor-only.** `_entrants_table.html` is shared with the
  public embed; `show_drift` is only ever set for editors, like `can_edit` for
  the payment column.

## Code Standards

- Do not add tests that are just testing django functionality
- `round` is used as a variable/parameter name throughout the pairing code (it refers to a tournament round). Do not rename it to avoid shadowing the builtin.
