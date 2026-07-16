# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Baxter is a scrabble tournament manager built with Django 6.0 and Python 3.14.

Design/implementation plans live in `plans/` (see `plans/README.md`) — put new
plans there, not in the repo root.

## Development Commands

```bash
# Install dependencies
uv sync            # Python deps
npm install        # JS deps (Tabulator for the edit grids) — served from
                   # node_modules via django-node-assets. Without it, dev grids
                   # 404 Tabulator and render blank (prod bakes it in via Dockerfile).

# Run development server
uv run python manage.py runserver

# Run migrations
uv run python manage.py migrate

# Create migrations after model changes
uv run python manage.py makemigrations

# Run tests
uv run python manage.py test

# Run a single test
uv run python manage.py test <app_name>.tests.TestClass.test_method

# Django management commands
uv run python manage.py <command>

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

## Configuration

Settings are managed via environment variables using python-decouple. Required variables are stored in `.env` (gitignored):
- `SECRET_KEY` - Django secret key
- `DEBUG` - Boolean, defaults to False
- `ALLOWED_HOSTS` - Comma-separated list of allowed hosts

## Architecture

Standard Django project structure:
- `baxter/` - Main Django project package (settings, urls, wsgi/asgi)
- `tournaments/` - Tournament management app
- `users/` - User authentication and management app
- `manage.py` - Django CLI entry point
- Database: SQLite (development), configurable for production via DATABASE_URL

## Code Standards

Follow web development best practices:
- Use Django's static files system for CSS/JS (not inline styles)
- Keep templates DRY with inheritance and includes
- Follow Django conventions for project structure
- Use class-based views (ListView, DetailView, CreateView, UpdateView, DeleteView)
- Use mixins for reusable view logic (e.g., permission checks)
- Do not add tests that are just testing django functionality
- `round` is used as a variable/parameter name throughout the pairing code (it refers to a tournament round). Do not rename it to avoid shadowing the builtin.
