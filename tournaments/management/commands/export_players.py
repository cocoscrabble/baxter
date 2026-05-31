import json

from django.core.management.base import BaseCommand

from tournaments.player_sync import export_players


class Command(BaseCommand):
    help = "Export all players as JSON for importing into another Baxter instance."

    def add_arguments(self, parser):
        parser.add_argument(
            "-o", "--output",
            help="Write the JSON to this file instead of stdout.",
        )

    def handle(self, *args, **options):
        data = export_players()
        text = json.dumps(data, indent=2)
        output = options.get("output")
        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(text)
            self.stdout.write(self.style.SUCCESS(
                f"Exported {len(data)} player(s) to {output}"
            ))
        else:
            self.stdout.write(text)
