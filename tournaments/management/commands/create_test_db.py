from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a test database with sample data"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Running migrations..."))
        call_command("migrate", verbosity=0)
        self.stdout.write(self.style.SUCCESS("Done\n"))

        self.stdout.write(self.style.MIGRATE_HEADING("Creating default user..."))
        call_command("create_default_user")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("Creating tournament..."))
        call_command("create_tournament", owner="emsworth")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("Importing players..."))
        call_command("import_players", "testdata/players.csv")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("Importing entrants..."))
        call_command("import_entrants", "testdata/entrants.csv", "1")
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("Test database created successfully!"))
