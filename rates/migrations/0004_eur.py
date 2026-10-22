from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0003_both_rates"),
    ]

    operations = [
        migrations.AddField(
            model_name="rateday",
            name="estr",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="fundingday",
            name="swaprate_eur",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
    ]
