from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("market", "0002_pair"),
    ]

    operations = [
        migrations.AddField(
            model_name="pair",
            name="future_scale",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
