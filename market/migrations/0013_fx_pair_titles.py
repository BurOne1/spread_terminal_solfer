"""Названия пар USD, CNY, EUR: по ногам, как в сообщениях алертов и списке спредов."""

from django.db import migrations

TITLES = {"USD": ("SI - USDRUBF", "доллар"), "CNY": ("CR - CNYRUBF", "юань"), "EUR": ("EU - EURRUBF", "евро")}


def forward(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    for code, (title, _old) in TITLES.items():
        Pair.objects.filter(code=code).update(title=title)


def backward(apps, schema_editor):
    Pair = apps.get_model("market", "Pair")
    for code, (_title, old) in TITLES.items():
        Pair.objects.filter(code=code).update(title=old)


class Migration(migrations.Migration):
    dependencies = [("market", "0012_minute_candle")]
    operations = [migrations.RunPython(forward, backward)]
