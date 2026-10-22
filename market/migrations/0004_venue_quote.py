from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0003_future_scale"),
    ]

    operations = [
        migrations.AddField(
            model_name="pair",
            name="venue",
            field=models.CharField(default="finam", max_length=16),
        ),
        migrations.AddField(
            model_name="pair",
            name="quote",
            field=models.CharField(default="RUB", max_length=8),
        ),
    ]
