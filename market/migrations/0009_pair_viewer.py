from django.db import migrations, models

# Зрителям по умолчанию: треугольник золота и валюты.
DEFAULT = ("TRIGD", "USD", "CNY", "EUR")


def forwards(apps, schema_editor):
    apps.get_model("market", "Pair").objects.filter(code__in=DEFAULT).update(viewer=True)


class Migration(migrations.Migration):
    dependencies = [("market", "0008_single_default_levels")]
    operations = [
        migrations.AddField(model_name="pair", name="viewer", field=models.BooleanField(default=False, verbose_name="для зрителей")),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
