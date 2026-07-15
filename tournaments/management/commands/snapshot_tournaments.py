"""Record a one-time state_snapshot for tournaments that predate the event log,
so they become replayable. Idempotent — a tournament that already has events is
skipped."""

from django.core.management.base import BaseCommand

from tournaments.events import snapshot_existing
from tournaments.models import Tournament


class Command(BaseCommand):
    help = "Backfill a state_snapshot event for tournaments with no event log."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only this tournament (default: all)")

    def handle(self, *args, **options):
        qs = Tournament.objects.all()
        if options.get("slug"):
            qs = qs.filter(slug=options["slug"])
        made = 0
        for tournament in qs:
            if snapshot_existing(tournament) is not None:
                made += 1
                self.stdout.write(f"snapshotted “{tournament}”")
        self.stdout.write(
            self.style.SUCCESS(f"Recorded {made} snapshot(s).")
        )
