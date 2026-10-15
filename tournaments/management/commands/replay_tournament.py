"""Replay a tournament's event log into the current database.

    manage.py replay_tournament path/to/export.jsonl --verify
    manage.py replay_tournament <slug> --from-slug --upto 12
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
            players = None
        else:
            with open(options["source"]) as fh:
                header, events = parse_jsonl(fh.read())
            players = header.get("players")

        # All or nothing: a replay that stops partway would otherwise leave a
        # half-built tournament behind, and the next attempt a second one.
        try:
            with transaction.atomic():
                ctx = replay(
                    events, verify=options["verify"], upto=options["upto"],
                    players=players,
                )
        except ReplayError as exc:
            raise CommandError(_with_notes(exc))
        except Exception as exc:
            # A command refusing an event (a player missing, a name taken) is a
            # replay failure too; report it with the seq it stopped at.
            raise CommandError(f"{type(exc).__name__}: {_with_notes(exc)}") from exc

        # Drift is not a failure: the published boards replayed as recorded.
        # It says today's engine would have printed something else — expected
        # after an engine fix, and worth knowing either way.
        for d in ctx.drift:
            self.stdout.write(self.style.WARNING(
                f"Engine drift: {d['division']} round {d['round']} was published "
                f"as recorded; today's engine would pair it differently."
            ))

        target = ctx.tournament
        suffix = " (digests verified)" if options["verify"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Replayed {ctx.applied} event(s) into “{target}”{suffix}"
                if target
                else f"Replayed {ctx.applied} event(s)"
            )
        )


def _with_notes(exc):
    return " ".join([str(exc), *getattr(exc, "__notes__", ())])
