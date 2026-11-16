from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rates", "0007_silver_funding")]

    operations = [
        migrations.AddField(
            model_name="fundingday", name="swaprate_btc",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name="fundingday", name="swaprate_eth",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
    ]
