# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Baxter is a scrabble tournament manager built with Django 6.0 and Python 3.14.

## Development Commands

```bash
# Install dependencies
uv sync

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

## Pairing engine (Rust cutover in progress)

The pairing computation is being cut over to the `scrabble-pairing` Rust crate,
called from Python via the `scrabble-pairing-py` PyO3 extension (a separate
crate so the core stays wasm-clean). `scrabble_pairing_py.pair_json(str) -> str`
is the boundary. Building requires a local Rust toolchain (already needed for
the crate); `uv sync` builds the wheel via maturin. See `PLAN_RUST_CUTOVER.md`.

Until the cutover completes, the both-engines policy still holds: pairing fixes
land in both `tournaments/pairing/` (Python) and `scrabble-pairing/` (Rust), and
must keep the parity corpus (`scrabble-pairing/tests/corpus/cases.json`) green
(`cargo test` in `scrabble-pairing/`).

Engine selection is `settings.PAIRING_ENGINE` (env `PAIRING_ENGINE`):
`python` (default), `rust`, or `shadow` (run both, return Python, log
divergences). Run the Django suite both ways:

```bash
uv run python manage.py test tournaments.tests
PAIRING_ENGINE=rust uv run python manage.py test tournaments.tests
```

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
