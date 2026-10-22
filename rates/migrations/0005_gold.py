from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0004_eur"),
    ]

    operations = [
        migrations.AddField(
            model_name="fundingday",
            name="swaprate_gold",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
    ]
