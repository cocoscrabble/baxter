from datetime import date

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import Division, Tournament
from users.models import User


class Command(BaseCommand):
    help = "Create a new tournament with divisions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default="Hood River 2023",
            help="Tournament name (default: Hood River 2023)",
        )
        parser.add_argument(
            "--location",
            default="Hood River, OR",
            help="Tournament location (default: Hood River, OR)",
        )
        parser.add_argument(
            "--date",
            default="2023-07-15",
            help="Start date YYYY-MM-DD (default: 2023-07-15)",
        )
        parser.add_argument(
            "--owner",
            required=True,
            help="Username of tournament owner",
        )
        parser.add_argument(
            "--divisions",
            default="Open,Novice",
            help="Comma-separated division names (default: Open,Novice)",
        )

    def handle(self, *args, **options):
        name = options["name"]
        location = options["location"]
        date_str = options["date"]
        owner_username = options["owner"]
        division_names = [d.strip() for d in options["divisions"].split(",")]

        # Parse date
        try:
            year, month, day = map(int, date_str.split("-"))
            start_date = date(year, month, day)
        except ValueError:
            raise CommandError(f"Invalid date format: {date_str}. Use YYYY-MM-DD")

        # Get owner
        try:
            owner = User.objects.get(username=owner_username)
        except User.DoesNotExist:
            raise CommandError(f"User not found: {owner_username}")

        # Check if tournament already exists
        if Tournament.objects.filter(name=name).exists():
            raise CommandError(f"Tournament already exists: {name}")

        # Create tournament
        tournament = Tournament.objects.create(
            name=name,
            location=location,
            start_date=start_date,
            owner=owner,
        )
        tournament.editors.add(owner)
        self.stdout.write(f"Created tournament: {name}")
        self.stdout.write(f"  Location: {location}")
        self.stdout.write(f"  Date: {start_date}")
        self.stdout.write(f"  Owner: {owner_username}")

        # Create divisions
        for division_name in division_names:
            Division.objects.create(name=division_name, tournament=tournament)
            self.stdout.write(f"  Division: {division_name}")

        self.stdout.write(
            self.style.SUCCESS(f"Successfully created tournament with {len(division_names)} divisions")
        )
