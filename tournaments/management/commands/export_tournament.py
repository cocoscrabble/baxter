from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Tournament
from tournaments.tournament_export import export_tournament


class Command(BaseCommand):
    help = "Export a tournament (divisions, entrants, results) as a JSON bundle for the registry."

    def add_arguments(self, parser):
        parser.add_argument("tournament_id", type=int, help="Tournament ID")
        parser.add_argument(
            "-o", "--output",
            help="Write the JSON to this file instead of stdout.",
        )

    def handle(self, *args, **options):
        try:
            tournament = Tournament.objects.get(pk=options["tournament_id"])
        except Tournament.DoesNotExist:
            raise CommandError(
                f"Tournament with ID {options['tournament_id']} does not exist"
            )

        text = export_tournament(tournament)
        output = options.get("output")
        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(text)
            self.stdout.write(self.style.SUCCESS(
                f"Exported tournament '{tournament.name}' to {output}"
            ))
        else:
            self.stdout.write(text)
