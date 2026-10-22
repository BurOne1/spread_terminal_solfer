from django.db import migrations, models


def copy_dfedtaru(apps, schema_editor):
    RateDay = apps.get_model("rates", "RateDay")
    for row in RateDay.objects.filter(foreign_source="DFEDTARU", foreign_rate__isnull=False):
        row.dfedtaru = row.foreign_rate
        row.save(update_fields=["dfedtaru"])


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0002_funding_usd"),
    ]

    operations = [
        migrations.AddField(
            model_name="rateday",
            name="shibor",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="rateday",
            name="dfedtaru",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True),
        ),
        migrations.RunPython(copy_dfedtaru, migrations.RunPython.noop),
    ]
