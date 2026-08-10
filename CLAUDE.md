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

## Code Standards

- Do not add tests that are just testing django functionality
- `round` is used as a variable/parameter name throughout the pairing code (it refers to a tournament round). Do not rename it to avoid shadowing the builtin.
