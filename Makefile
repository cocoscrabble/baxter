.PHONY: run test test-all rust-engine

run:
	uv run python manage.py runserver

test:
	uv run python manage.py test tournaments.tests --exclude-tag slow

test-all:
	uv run python manage.py test tournaments.tests

# Rebuild the scrabble-pairing-py extension after editing the Rust crates. uv
# does not watch the crate source, so this must be run by hand.
rust-engine:
	uv sync --reinstall-package scrabble-pairing-py
