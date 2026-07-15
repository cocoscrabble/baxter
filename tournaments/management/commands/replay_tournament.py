"""Replay a tournament's event log into the current database.

    manage.py replay_tournament path/to/export.jsonl --verify
    manage.py replay_tournament <slug> --from-slug --upto 12
"""

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Tournament
from tournaments.replay import (
    ReplayError,
    events_from_tournament,
    parse_jsonl,
    replay,
)


class Command(BaseCommand):
    help = "Replay a tournament event log (JSONL file, or a live slug) into this DB."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Path to a JSONL export, or a tournament slug")
        parser.add_argument(
            "--from-slug",
            action="store_true",
            help="Treat SOURCE as a live tournament slug instead of a file",
        )
        parser.add_argument("--upto", type=int, default=None, help="Stop after this seq")
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Compare recorded digests to the replayed state",
        )

    def handle(self, *args, **options):
        if options["from_slug"]:
            try:
                tournament = Tournament.objects.get(slug=options["source"])
            except Tournament.DoesNotExist:
                raise CommandError(f"No tournament with slug {options['source']!r}")
            events = events_from_tournament(tournament)
        else:
            with open(options["source"]) as fh:
                _header, events = parse_jsonl(fh.read())

        try:
            ctx = replay(events, verify=options["verify"], upto=options["upto"])
        except ReplayError as exc:
            raise CommandError(str(exc))

        target = ctx.tournament
        suffix = " (digests verified)" if options["verify"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Replayed {len(events)} event(s) into “{target}”{suffix}"
                if target
                else f"Replayed {len(events)} event(s)"
            )
        )
