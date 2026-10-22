from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0004_venue_quote"),
        ("rates", "0005_gold"),
    ]

    operations = [
        migrations.CreateModel(
            name="FundingPoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ts", models.DateTimeField()),
                ("rate", models.DecimalField(decimal_places=10, max_digits=18)),
                ("source", models.CharField(default="bybit", max_length=32)),
                ("pair", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="funding_points", to="market.pair")),
            ],
            options={
                "verbose_name": "точка фандинга",
                "verbose_name_plural": "точки фандинга",
                "ordering": ["pair", "ts"],
            },
        ),
        migrations.AddConstraint(
            model_name="fundingpoint",
            constraint=models.UniqueConstraint(fields=("pair", "ts"), name="uniq_funding_point"),
        ),
    ]
