import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Division, Entrant, Player


class Command(BaseCommand):
    help = "Import entrants from a CSV file into a division"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to CSV file with Name,Rating columns")
        parser.add_argument("division_id", type=int, help="Division ID to add entrants to")

    def handle(self, *args, **options):
        csv_file = options["csv_file"]
        division_id = options["division_id"]

        # Get the division
        try:
            division = Division.objects.get(pk=division_id)
        except Division.DoesNotExist:
            raise CommandError(f"Division with ID {division_id} does not exist")

        # Read CSV file
        try:
            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_file}")

        if not rows:
            raise CommandError("CSV file is empty")

        # Look up all players and collect errors
        players_with_ratings = []
        missing_players = []

        for row in rows:
            name = row["Name"].strip()
            rating = int(row["Rating"])

            try:
                player = Player.objects.get(name=name)
                players_with_ratings.append((player, rating))
            except Player.DoesNotExist:
                missing_players.append(name)

        # If any players are missing, print error and exit
        if missing_players:
            self.stderr.write(self.style.ERROR("The following players were not found:"))
            for name in missing_players:
                self.stderr.write(self.style.ERROR(f"  - {name}"))
            sys.exit(1)

        # Clear existing entrants for this division
        existing_count = division.entrants.count()
        if existing_count > 0:
            self.stdout.write(
                self.style.WARNING(f"Removing {existing_count} existing entrants from division")
            )
            division.entrants.all().delete()

        # Enter everyone, then seed them. This used to sort by rating and number
        # 1..n itself, which was a fourth copy of a rule that lives in
        # Entrant.seeding_for — and one with no tiebreak, so equal ratings came
        # out in file order.
        for player, _rating in players_with_ratings:
            Entrant.enter(division, player, Entrant.next_number(division))
        Entrant.apply_seeding(division, Entrant.seeding_for(division))

        for entrant in division.entrants.select_related("player").order_by("number"):
            self.stdout.write(
                f"  {entrant.number}. {entrant.player.name} ({entrant.rating})"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully imported {len(players_with_ratings)} entrants into {division.name}"
            )
        )
