from django.db import migrations, models
from django.db.models import F


def keep_existing_scoring(apps, schema_editor):
    # Until now a forfeit was charged the bye spread. Existing divisions keep
    # scoring that way; only new ones get the separate default.
    DivisionSettings = apps.get_model("tournaments", "DivisionSettings")
    DivisionSettings.objects.update(forfeit_spread=F("bye_spread"))


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0054_rated_games"),
    ]

    operations = [
        migrations.AddField(
            model_name="divisionsettings",
            name="forfeit_spread",
            field=models.IntegerField(default=100),
        ),
        migrations.RunPython(keep_existing_scoring, migrations.RunPython.noop),
    ]
