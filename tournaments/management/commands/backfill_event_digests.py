"""Rewrite stored event digests from the v1 (name-keyed) form to v2.

A migration used to do this, renumbered after every schema change to stay last
(it replays through the *live* models, so it can only run once every migration
has been applied). Once production had run it, it was retired (2026-10-15): a
new database writes v2 digests from the start, so only an old database copy
that never ran it still needs this. Apply the migrations, then run this.

Safe to re-run: a tournament whose digests are already v2 verifies under v1,
fails to match, and is reported as skipped rather than rewritten twice.
"""

from django.core.management.base import BaseCommand, CommandError

from tournaments.digest_backfill import backfill_all, schema_mismatch


class Command(BaseCommand):
    help = "Backfill TournamentEvent digests to the number-keyed (v2) form."

    def handle(self, *args, **options):
        mismatch = schema_mismatch()
        if mismatch:
            raise CommandError(
                f"The database is behind the models: {mismatch}. "
                f"Run `manage.py migrate` first."
            )
        done, skipped = backfill_all(log=self.stdout.write)
        self.stdout.write(
            self.style.SUCCESS(
                f"{done} tournament(s) rewritten, {skipped} skipped"
            )
        )
