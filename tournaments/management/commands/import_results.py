import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Division, Entrant, ResultSlip


class Command(BaseCommand):
    help = "Import result slips from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to CSV file")
        parser.add_argument("division_id", type=int, help="Division ID")

    def handle(self, *args, **options):
        csv_file = options["csv_file"]
        division_id = options["division_id"]

        # Get the division
        try:
            division = Division.objects.get(pk=division_id)
        except Division.DoesNotExist:
            raise CommandError(f"Division with ID {division_id} does not exist")

        # Build a lookup of entrants by player name
        entrants_by_name = {}
        for entrant in division.entrants.select_related("player").all():
            entrants_by_name[entrant.player.name] = entrant

        # Read CSV file (utf-8-sig handles BOM)
        try:
            with open(csv_file, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_file}")

        if not rows:
            raise CommandError("CSV file is empty")

        # Process rows and collect errors
        missing_players = set()
        results_to_create = []

        for row in rows:
            round_num = int(row["Round"])
            winner_name = row["Winner"].strip()
            winner_score = int(row["Winners Score"])
            loser_name = row["Opponent"].strip()
            loser_score = int(row["Opponents Score"])

            winner = entrants_by_name.get(winner_name)
            loser = entrants_by_name.get(loser_name)

            if not winner:
                missing_players.add(winner_name)
            if not loser:
                missing_players.add(loser_name)

            if winner and loser:
                results_to_create.append({
                    "division": division,
                    "round": round_num,
                    "winner": winner,
                    "winner_score": winner_score,
                    "loser": loser,
                    "loser_score": loser_score,
                    "winner_started": False,  # Not in CSV, default to False
                })

        # If any players are missing, print error and exit
        if missing_players:
            self.stderr.write(self.style.ERROR("The following players were not found as entrants:"))
            for name in sorted(missing_players):
                self.stderr.write(self.style.ERROR(f"  - {name}"))
            sys.exit(1)

        # Clear existing result slips for this division
        existing_count = division.result_slips.count()
        if existing_count > 0:
            self.stdout.write(
                self.style.WARNING(f"Removing {existing_count} existing result slips")
            )
            division.result_slips.all().delete()

        # Create result slips
        for result in results_to_create:
            ResultSlip.objects.create(**result)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully imported {len(results_to_create)} result slips into {division.name}"
            )
        )
