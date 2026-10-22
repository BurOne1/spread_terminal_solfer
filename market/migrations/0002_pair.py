import django.db.models.deletion
from django.db import migrations, models


def seed_cny(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    Instrument = apps.get_model("market", "Instrument")
    pair, _ = Pair.objects.get_or_create(
        code="CNY",
        defaults={
            "title": "юань",
            "future_root": "CR",
            "perp_code": "CNYRUBF",
            "perp_symbol": "CNYRUBF@RTSX",
            "spot_code": "CNYRUB_TOM",
            "spot_symbol": "CNYRUB_TOM@MISX",
            "mic": "RTSX",
            "foreign_source": "SHIBOR",
            "lot": 1000,
            "long_levels": [-0.138, -0.20, -0.27],
            "short_levels": [0.138, 0.20, 0.27],
            "long_tp": [-0.03, 0.0, 0.0],
            "short_tp": [0.03, 0.0, 0.0],
            "active": True,
        },
    )
    Instrument.objects.filter(pair__isnull=True).update(pair=pair)


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Pair",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=8, unique=True)),
                ("title", models.CharField(max_length=64)),
                ("future_root", models.CharField(max_length=8)),
                ("perp_code", models.CharField(max_length=16)),
                ("perp_symbol", models.CharField(max_length=64)),
                ("spot_code", models.CharField(max_length=16)),
                ("spot_symbol", models.CharField(max_length=64)),
                ("mic", models.CharField(default="RTSX", max_length=8)),
                ("foreign_source", models.CharField(default="SHIBOR", max_length=32)),
                ("lot", models.PositiveIntegerField(default=1000)),
                ("long_levels", models.JSONField(default=list)),
                ("short_levels", models.JSONField(default=list)),
                ("long_tp", models.JSONField(default=list)),
                ("short_tp", models.JSONField(default=list)),
                ("active", models.BooleanField(default=True)),
            ],
            options={"verbose_name": "пара", "verbose_name_plural": "пары", "ordering": ["code"]},
        ),
        migrations.AddField(
            model_name="instrument",
            name="listed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="instrument",
            name="pair",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="instruments",
                to="market.pair",
            ),
        ),
        migrations.RunPython(seed_cny, migrations.RunPython.noop),
    ]
