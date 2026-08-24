# Phase 3d of plans/PLAN_PLAYER_IDENTITY.md. See tournaments/digest_backfill.py
# for what this does and why it is allowed to touch an append-only log.

from django.db import migrations


def backfill(apps, schema_editor):
    # Deliberately the *live* models rather than the historical ones: the work
    # is a full replay through the real command layer, which is not expressible
    # against apps.get_model(). That is safe here only because this migration
    # adds no schema change of its own and runs last — the models it imports are
    # the models it is written against.
    from tournaments.digest_backfill import backfill_all

    lines = []
    done, skipped = backfill_all(log=lines.append)
    if lines:
        print("\nEvent digest backfill (v1 -> v2):")
        for line in lines:
            print(line)
        print(f"  {done} tournament(s) rewritten, {skipped} skipped")


def noop_reverse(apps, schema_editor):
    """Not reversible: the v1 digests are gone once overwritten, and
    recomputing them would need the v1 code that this change replaced."""


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0037_playoff_seeds_keyed_by_number"),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
