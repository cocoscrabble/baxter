# Phase 2 of plans/PLAN_PLAYER_IDENTITY.md: the playoff bracket derives from
# player numbers, so the frozen seed snapshot has to carry them.

from django.db import migrations


def add_keys(apps, schema_editor):
    """Give every recorded seed a ``key`` alongside its ``player`` name.

    Resolving by name is exact for these rows: they were all written while the
    schema enforced globally unique player names. A seed whose name no longer
    matches an entrant (the entrant was removed after the playoff was created)
    keeps the name as its key — the bracket already treated it as a
    non-participant, and inventing a number here would be worse than leaving
    the row visibly unresolved.
    """
    Playoff = apps.get_model("tournaments", "Playoff")
    for playoff in Playoff.objects.select_related("division").iterator():
        seeds = playoff.seeds or []
        if not seeds or all(s.get("key") for s in seeds):
            continue
        by_name = {
            e.player.name: e.player.player_number
            for e in playoff.division.entrants.select_related("player")
        }
        playoff.seeds = [
            s if s.get("key") else {**s, "key": by_name.get(s["player"], s["player"])}
            for s in seeds
        ]
        playoff.save(update_fields=["seeds"])


def drop_keys(apps, schema_editor):
    Playoff = apps.get_model("tournaments", "Playoff")
    for playoff in Playoff.objects.iterator():
        seeds = playoff.seeds or []
        if not any(s.get("key") for s in seeds):
            continue
        playoff.seeds = [{k: v for k, v in s.items() if k != "key"} for s in seeds]
        playoff.save(update_fields=["seeds"])


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0036_player_number_identity"),
    ]

    operations = [
        migrations.RunPython(add_keys, drop_keys),
    ]
