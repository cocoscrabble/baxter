"""Export a tournament's event log as JSONL (for shell / dokku use)."""

from django.core.management.base import BaseCommand, CommandError

from tournaments.events import export_jsonl
from tournaments.models import Tournament


class Command(BaseCommand):
    help = "Export a tournament's event log as JSONL to stdout or a file."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Tournament slug")
        parser.add_argument(
            "-o", "--output", help="Write to this file instead of stdout"
        )

    def handle(self, *args, **options):
        try:
            tournament = Tournament.objects.get(slug=options["slug"])
        except Tournament.DoesNotExist:
            raise CommandError(f"No tournament with slug {options['slug']!r}")
        content = export_jsonl(tournament)
        if options.get("output"):
            with open(options["output"], "w") as fh:
                fh.write(content)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Wrote {tournament.events.count()} events to {options['output']}"
                )
            )
        else:
            self.stdout.write(content, ending="")
