from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rates", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fundingday",
            name="swaprate_rub",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name="fundingday",
            name="swaprate_usd",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
    ]
