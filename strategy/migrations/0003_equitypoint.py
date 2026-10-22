from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0002_pair"),
        ("strategy", "0002_spreadbar_pair"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquityPoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ts", models.DateTimeField()),
                ("contract", models.CharField(max_length=16)),
                ("equity", models.DecimalField(decimal_places=2, max_digits=18)),
                ("marker", models.CharField(blank=True, max_length=8)),
                ("pair", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="equity", to="market.pair")),
            ],
            options={"ordering": ["ts"], "verbose_name": "точка PNL", "verbose_name_plural": "точки PNL"},
        ),
        migrations.AddConstraint(
            model_name="equitypoint",
            constraint=models.UniqueConstraint(fields=("pair", "ts"), name="uniq_equity_pair_ts"),
        ),
    ]
