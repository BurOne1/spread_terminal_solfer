"""«Для просмотра»: треугольники на споте вместо TRIGD — TRIGDS и TRISVS.

TRISVS, если его ещё нет, отметит команда add_trisvs при создании.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    Pair.objects.filter(code="TRIGD").update(viewer=False)
    Pair.objects.filter(code__in=("TRIGDS", "TRISVS")).update(viewer=True)


class Migration(migrations.Migration):
    dependencies = [("market", "0010_pair_viewer_label")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
