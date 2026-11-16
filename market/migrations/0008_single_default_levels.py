"""Уровни пар по умолчанию: один вход и один тейк на сторону вместо лесенки.

Из сохранённых у пары списков остаётся первый: ближний вход и первый тейк.
Свои уровни в браузере пользователя миграция не трогает.
"""

from django.db import migrations

FIELDS = ("long_levels", "short_levels", "long_tp", "short_tp")


def first(value):
    if isinstance(value, dict) and isinstance(value.get("values"), list) and len(value["values"]) > 1:
        return {**value, "values": value["values"][:1]}
    if isinstance(value, list) and len(value) > 1:
        return value[:1]
    return value


def forwards(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    for pair in Pair.objects.all():
        changed = {field: first(getattr(pair, field)) for field in FIELDS}
        if any(changed[field] != getattr(pair, field) for field in FIELDS):
            Pair.objects.filter(pk=pair.pk).update(**changed)


class Migration(migrations.Migration):
    dependencies = [("market", "0007_ngcal_absolute_levels")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
