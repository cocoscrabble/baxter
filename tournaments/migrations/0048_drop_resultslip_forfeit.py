# plans/PLAN_FORFEITS.md: the flag never became load-bearing. A forfeit always
# carries the bye entrant, so ResultSlipQuerySet.played() recognises one without
# it, and the unscored-game case it was held for needs an engine change instead.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0047_withdrawal_settings'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='resultslip',
            name='forfeit',
        ),
    ]
