# Phase 3d of plans/PLAN_PLAYER_IDENTITY.md. See tournaments/digest_backfill.py
# for what this does and why it is allowed to touch an append-only log.

from django.db import migrations


def backfill(apps, schema_editor):
    # Deliberately the *live* models rather than the historical ones: the work is
    # a full replay through the real command layer, which is not expressible
    # against apps.get_model(). That means it can only run when the live models
    # match the schema — i.e. when this is the last migration to apply.
    #
    # It is not enough to *be* last today. An earlier draft of this file ran as
    # 0038, and adding the entrant fields after it made every tournament fail
    # with "no such column: tournaments_player.wespa_rating" — silently leaving
    # every digest at v1. So the check below is explicit, and refuses rather
    # than skipping: a backfill that quietly does nothing is worse than one that
    # stops the deploy and tells you what to run.
    #
    # **If you add a schema migration, renumber this one to sit after it.** That
    # has now happened three times (0038 -> 0041 -> 0042 -> 0045). It is a one-time
    # transitional migration that will eventually be squashed away, and the
    # check makes getting it wrong loud rather than silent — but the rule is
    # this, and there is no way to express "always last" in Django.
    from tournaments.digest_backfill import backfill_all, schema_mismatch

    mismatch = schema_mismatch()
    if mismatch:
        raise RuntimeError(
            f"Cannot backfill event digests: {mismatch}. This migration replays "
            f"through the live models, so it must be the last one to apply. "
            f"Move it after the newer migrations, or apply them first and then "
            f"run `manage.py backfill_event_digests`."
        )

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
        ("tournaments", "0044_wespa_mirror"),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
