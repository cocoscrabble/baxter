import csv

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Player


class Command(BaseCommand):
    help = "Import players from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to CSV file with Name,Number,Rating columns")
        parser.add_argument(
            "--update",
            action="store_true",
            help="Update existing players instead of skipping them",
        )

    def handle(self, *args, **options):
        csv_file = options["csv_file"]
        update = options["update"]

        # Read CSV file
        try:
            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_file}")

        if not rows:
            raise CommandError("CSV file is empty")

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for row in rows:
            name = row["Name"].strip()
            player_number = row["Number"].strip()
            rating = int(row["Rating"])

            existing = Player.objects.filter(name=name).first()

            if existing:
                if update:
                    existing.player_number = player_number
                    existing.rating = rating
                    existing.save()
                    updated_count += 1
                    self.stdout.write(f"  Updated: {name}")
                else:
                    skipped_count += 1
            else:
                Player.objects.create(
                    name=name,
                    player_number=player_number,
                    rating=rating,
                )
                created_count += 1
                self.stdout.write(f"  Created: {name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {created_count} created, {updated_count} updated, {skipped_count} skipped"
            )
        )
