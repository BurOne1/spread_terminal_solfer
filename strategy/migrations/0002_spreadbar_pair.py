import django.db.models.deletion
from django.db import migrations, models


def attach_pair(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    SpreadBar = apps.get_model("strategy", "SpreadBar")
    pair = Pair.objects.filter(code="CNY").first()
    if pair is not None:
        SpreadBar.objects.filter(pair__isnull=True).update(pair=pair)


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0002_pair"),
        ("strategy", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="spreadbar",
            name="pair",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="spread_bars",
                to="market.pair",
            ),
        ),
        migrations.RunPython(attach_pair, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="spreadbar",
            name="ts",
            field=models.DateTimeField(),
        ),
        migrations.AddConstraint(
            model_name="spreadbar",
            constraint=models.UniqueConstraint(fields=("pair", "ts"), name="uniq_spreadbar_pair_ts"),
        ),
    ]
