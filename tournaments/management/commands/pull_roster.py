"""Pull the central roster, unattended.

The scheduled counterpart to the admin page at ``/players/roster/``: same fetch,
same importer, same held-back rows. This is what ``app.json``'s cron entry runs,
and what you run by hand on a box with no browser.
"""

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import RosterSync
from tournaments.roster_sync import run_sync


class Command(BaseCommand):
    help = "Pull the central player roster and apply it (see plans/PLAN_COCO_PROGRAM.md)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            help="Import this snapshot file instead of fetching from the endpoint.",
        )
        parser.add_argument(
            "--source",
            choices=[s for s, _ in RosterSync.SOURCES],
            help="How to record this run. Defaults to 'scheduled', or 'upload' with --file.",
        )

    def handle(self, *args, **options):
        path = options.get("file")
        raw = None
        if path:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as exc:
                raise CommandError(f"Could not read {path}: {exc}") from None

        source = options.get("source") or (
            RosterSync.UPLOAD if path else RosterSync.SCHEDULED
        )
        record = run_sync(source, raw)

        if not record.ok:
            # Non-zero, so cron notices. The RosterSync row is written either
            # way — the exit code is for the machine, the row is for the human.
            raise CommandError(record.error)

        self.stdout.write(self.style.SUCCESS(record.summary()))
        if record.pending:
            # Not a failure: these are correct outcomes that need a human. The
            # pull wrote nothing for them, and they wait on the record until a
            # director confirms each at /players/roster/.
            self.stdout.write(
                f"{len(record.pending)} player(s) look like guests who have since "
                f"been given a number. Nothing was changed for them — confirm "
                f"each at /players/roster/:"
            )
            for entry in record.pending:
                self.stdout.write(
                    f"  {entry['name']}: {entry['local_number']} -> "
                    f"{entry['roster_number']}"
                )
