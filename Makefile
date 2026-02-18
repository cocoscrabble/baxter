.PHONY: run test

run:
	uv run python manage.py runserver

test:
	uv run python manage.py test tournaments.tests --exclude-tag slow

test-all:
	uv run python manage.py test tournaments.tests
