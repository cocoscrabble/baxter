from django.db import migrations, models
import django.db.models.deletion
from django.utils.text import slugify

RESERVED_SLUGS = {"create", "fake-tournament", "create-player", "players"}


def _gen(base, taken, max_length, fallback):
    root = slugify(base)[:max_length] or fallback
    candidate = root
    n = 2
    while candidate in RESERVED_SLUGS or candidate in taken:
        suffix = f"-{n}"
        candidate = f"{root[:max_length - len(suffix)]}{suffix}"
        n += 1
    taken.add(candidate)
    return candidate


def backfill_slugs(apps, schema_editor):
    Tournament = apps.get_model("tournaments", "Tournament")
    Division = apps.get_model("tournaments", "Division")

    taken = set()
    for t in Tournament.objects.all().order_by("pk"):
        t.slug = _gen(t.name, taken, 220, "tournament")
        t.save(update_fields=["slug"])

    by_tournament = {}
    for d in Division.objects.all().order_by("pk"):
        taken_t = by_tournament.setdefault(d.tournament_id, set())
        d.slug = _gen(d.name, taken_t, 120, "division")
        d.save(update_fields=["slug"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0027_streamed_table_labels"),
    ]

    operations = [
        migrations.CreateModel(
            name="TournamentSlugAlias",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=220, unique=True)),
                ("tournament", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slug_aliases", to="tournaments.tournament")),
            ],
        ),
        migrations.CreateModel(
            name="DivisionSlugAlias",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=120)),
                ("division", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slug_aliases", to="tournaments.division")),
                ("tournament", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="division_slug_aliases", to="tournaments.tournament")),
            ],
            options={"unique_together": {("tournament", "slug")}},
        ),
        migrations.AddField(
            model_name="tournament",
            name="slug",
            field=models.SlugField(default="", editable=False, max_length=220),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="division",
            name="slug",
            field=models.SlugField(default="", editable=False, max_length=120),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_slugs, noop),
        migrations.AlterField(
            model_name="tournament",
            name="slug",
            field=models.SlugField(editable=False, max_length=220, unique=True),
        ),
        migrations.AlterUniqueTogether(
            name="division",
            unique_together={("tournament", "name"), ("tournament", "slug")},
        ),
    ]
