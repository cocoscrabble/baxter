from django.db import migrations, models


def copy_table_number_to_label(apps, schema_editor):
    FixedTable = apps.get_model("tournaments", "FixedTable")
    for ft in FixedTable.objects.all():
        ft.table_label = str(ft.table_number)
        ft.save(update_fields=["table_label"])


def copy_table_label_to_number(apps, schema_editor):
    FixedTable = apps.get_model("tournaments", "FixedTable")
    for ft in FixedTable.objects.all():
        try:
            ft.table_number = int(ft.table_label)
        except (TypeError, ValueError):
            ft.table_number = 0
        ft.save(update_fields=["table_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0026_alter_entrant_options_alter_entrant_managers_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pairing",
            name="table_label",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
        migrations.AddField(
            model_name="fixedtable",
            name="table_label",
            field=models.CharField(default="", max_length=8),
            preserve_default=False,
        ),
        migrations.RunPython(
            copy_table_number_to_label, copy_table_label_to_number
        ),
        migrations.RemoveField(
            model_name="fixedtable",
            name="table_number",
        ),
        migrations.AlterModelOptions(
            name="fixedtable",
            options={"ordering": ["round_number", "table_label"]},
        ),
    ]
