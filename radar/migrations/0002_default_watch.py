"""Стартовый список сверх ног спредов. Правится в админке."""

from django.db import migrations

DEFAULTS = (
    ("RI", "quarter", "RTS"),
    ("MX", "quarter", "MIX"),
    ("IMOEXF@RTSX", "symbol", "IMOEXF, вечный на индекс"),
)


def add(apps, schema_editor):
    WatchItem = apps.get_model("radar", "WatchItem")
    for code, kind, title in DEFAULTS:
        WatchItem.objects.get_or_create(code=code, defaults={"kind": kind, "title": title})


def remove(apps, schema_editor):
    WatchItem = apps.get_model("radar", "WatchItem")
    WatchItem.objects.filter(code__in=[code for code, _kind, _title in DEFAULTS]).delete()


class Migration(migrations.Migration):
    dependencies = [("radar", "0001_initial")]
    operations = [migrations.RunPython(add, remove)]
