# Phase 1d of plans/PLAN_ENTRANTS.md: existing entrants get the rating snapshot
# they would have been given had they been entered through Entrant.enter.

from django.db import migrations


def backfill(apps, schema_editor):
    """Pin each existing entrant's rating to their player's current one.

    Only the CoCo rating exists at this point — ``wespa_rating`` starts NULL
    everywhere — so the cascade collapses to "CoCo if they have one, else
    nothing". The booleans and the note keep their field defaults; there is no
    historical registration state to recover, and inventing one would be worse
    than recording that it was never captured.

    The bye entrant is included and lands on rating 0 / source ``none``, which is
    exactly what ``Division.bye_entrant`` would produce today.
    """
    Entrant = apps.get_model("tournaments", "Entrant")
    updated = []
    for entrant in Entrant.objects.select_related("player").iterator():
        entrant.rating = entrant.player.rating
        entrant.rating_source = "coco" if entrant.player.rating else "none"
        updated.append(entrant)
        if len(updated) >= 500:
            Entrant.objects.bulk_update(updated, ["rating", "rating_source"])
            updated = []
    if updated:
        Entrant.objects.bulk_update(updated, ["rating", "rating_source"])


def unbackfill(apps, schema_editor):
    """Reversible: the snapshot is derived, so dropping it loses nothing that
    0040 did not itself put there."""
    Entrant = apps.get_model("tournaments", "Entrant")
    Entrant.objects.update(rating=0, rating_source="none")


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0039_entrant_registration_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
