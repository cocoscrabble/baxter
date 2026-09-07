# plans/PLAN_FORFEITS.md: the withdrawal policy is a division rule, not a
# per-entrant flag, so Entrant.forfeits (added in 0045) moves to
# DivisionSettings alongside the jurisdictional bye spread.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0046_pairing_forfeit'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='entrant',
            name='forfeits',
        ),
        migrations.AddField(
            model_name='divisionsettings',
            name='bye_spread',
            field=models.IntegerField(default=50),
        ),
        migrations.AddField(
            model_name='divisionsettings',
            name='withdrawal',
            field=models.CharField(choices=[('omit', 'Leave the rounds blank'), ('forfeit', 'Record forfeit losses')], default='omit', max_length=16),
        ),
    ]
