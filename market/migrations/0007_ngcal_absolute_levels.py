"""NGCAL: уровни стали абсолютными ($/MMBtu) вместо долей цены фронта.

Уровни по умолчанию из add_extra_spreads (2 % цены) заменяются на 0.07 $.
Свои уровни, отличные от умолчания, не трогаются: их нужно задать заново.
"""

from django.db import migrations

OLD = {
    "long_levels": {"signed": True, "values": [-0.02, -0.04, -0.06]},
    "short_levels": {"signed": True, "values": [0.02, 0.04, 0.06]},
    "long_tp": {"signed": True, "values": [0.005, 0.0]},
    "short_tp": {"signed": True, "values": [-0.005, 0.0]},
}
NEW = {
    "long_levels": {"signed": True, "values": [-0.07, -0.14, -0.21]},
    "short_levels": {"signed": True, "values": [0.07, 0.14, 0.21]},
    "long_tp": {"signed": True, "values": [0.02, 0.0]},
    "short_tp": {"signed": True, "values": [-0.02, 0.0]},
}


def close(a, b):
    try:
        return a.get("signed") == b["signed"] and len(a["values"]) == len(b["values"]) and all(
            abs(x - y) < 1e-9 for x, y in zip(a["values"], b["values"]))
    except (AttributeError, KeyError, TypeError):
        return False


def forwards(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    for pair in Pair.objects.filter(foreign_source="LISTED"):
        if all(close(getattr(pair, field), value) for field, value in OLD.items()):
            Pair.objects.filter(pk=pair.pk).update(**NEW)


class Migration(migrations.Migration):
    dependencies = [("market", "0006_instrument_foreign")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
