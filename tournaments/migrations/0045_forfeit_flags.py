# Phase 1 of plans/PLAN_FORFEITS.md: the two flags, set by nobody yet.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0044_wespa_mirror'),
    ]

    operations = [
        migrations.AddField(
            model_name='entrant',
            name='forfeits',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='resultslip',
            name='forfeit',
            field=models.BooleanField(default=False),
        ),
    ]
