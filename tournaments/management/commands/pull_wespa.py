"""Pull the WESPA rating list, unattended.

The scheduled counterpart to the admin page at ``/players/wespa/``: same fetch,
same importer, same held-back links. This is what ``app.json``'s cron entry runs,
and what you run by hand on a box with no browser.
"""

from django.core.management.base import BaseCommand, CommandError

from tournaments.models import WespaSync
from tournaments.wespa_sync import run_sync


class Command(BaseCommand):
    help = "Pull the WESPA rating list and apply it (see plans/PLAN_WESPA.md)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            help="Import this file instead of fetching from the endpoint.",
        )
        parser.add_argument(
            "--source",
            choices=[s for s, _ in WespaSync.SOURCES],
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
            WespaSync.UPLOAD if path else WespaSync.SCHEDULED
        )
        record = run_sync(source, raw)

        if not record.ok:
            # Non-zero, so cron notices. The WespaSync row is written either
            # way — the exit code is for the machine, the row is for the human.
            raise CommandError(record.error)

        self.stdout.write(self.style.SUCCESS(record.summary()))
        if record.pending:
            # Not a failure: these are correct outcomes that need a human. The
            # pull wrote nothing for them, and they wait on the record until a
            # director resolves each at /players/wespa/.
            self.stdout.write(
                f"{len(record.pending)} name(s) are ambiguous. Nothing was "
                f"changed for them — resolve each at /players/wespa/:"
            )
            for entry in record.pending:
                self.stdout.write(
                    f"  {entry['name']}: {len(entry['players'])} local, "
                    f"{len(entry['candidates'])} in the list"
                )
