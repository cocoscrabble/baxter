# Phase 2 of plans/PLAN_FORFEITS.md: a bye-shaped row that is a forfeit.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0045_forfeit_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='pairing',
            name='forfeit',
            field=models.BooleanField(default=False),
        ),
    ]
