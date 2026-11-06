"""Общие правила сигналов и перенос порогов из инструментов радара.

Значения — копия radar.rules.DEFAULTS на момент миграции: код правил может
меняться, а миграция должна давать тот же результат.
"""

from django.db import migrations

DEFAULTS = {
    "large": {"enabled": True, "threshold": 99.5, "ratio": 3.0, "cooldown_seconds": 0},
    "sweep": {"enabled": True, "threshold": 99.0, "levels": 3, "gap_ms": 100, "cooldown_seconds": 20},
    "wall": {"enabled": True, "threshold": 99.5, "ratio": 5.0, "confirm_seconds": 30, "cooldown_seconds": 60},
    "pulled": {"enabled": True, "levels": 3, "cooldown_seconds": 60},
    "eaten": {"enabled": True, "ratio": 0.5, "cooldown_seconds": 60},
    "volume": {"enabled": True, "threshold": 5.0, "ratio": 3.0, "window_minutes": 1, "cooldown_seconds": 60},
    "oi": {"enabled": True, "threshold": 6.0, "window_minutes": 1, "cooldown_seconds": 60},
}


def scope_of(item):
    """Охват правила для строки инструмента: корень квартала или символ."""
    return item.code


def add(apps, schema_editor):
    SignalRule = apps.get_model("radar", "SignalRule")
    WatchItem = apps.get_model("radar", "WatchItem")
    for kind, values in DEFAULTS.items():
        SignalRule.objects.get_or_create(kind=kind, scope="", defaults=values)
    for item in WatchItem.objects.all():
        if item.large_pct != 99.5:
            SignalRule.objects.get_or_create(
                kind="large", scope=scope_of(item),
                defaults={"threshold": item.large_pct, "note": "перенесено из инструментов радара"},
            )
        if item.wall_ratio != 5.0:
            SignalRule.objects.get_or_create(
                kind="wall", scope=scope_of(item),
                defaults={"ratio": item.wall_ratio, "note": "перенесено из инструментов радара"},
            )


def remove(apps, schema_editor):
    apps.get_model("radar", "SignalRule").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("radar", "0005_signal_rule")]
    operations = [migrations.RunPython(add, remove)]
