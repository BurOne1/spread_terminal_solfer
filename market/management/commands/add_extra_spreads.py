"""Пары CRCNH, SI6E, NGCAL и TRIGDS.

CRCNH  CR × CNH × 1000 − Si   рубли за 1000 долларов (пункты Si), CNH — CME
SI6E   Si × 6E − Eu           рубли за 1000 евро (пункты Eu), 6E — CME
NGCAL  NGX6NGZ6@RTSX          календарь газа Мосбиржи одним инструментом, $/MMBtu
TRIGDS GD × SI − GLDRUB_TOM   как TRIGD, но третья нога — спот золота, без фандинга

Кросс-пары и NGCAL — справедливый ноль, уровни — доли цены Si, Eu или фронта NG.
Лот кросс-пары — один контракт Si или Eu: PnL в рублях на него. Лот NGCAL —
100 MMBtu, один календарь. TRIGDS берёт уровни TRIGD.
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from market import cross, ngcal
from market.models import Pair


def levels(pct, tp):
    return {
        "long_levels": {"signed": True, "values": [-pct, -2 * pct, -3 * pct]},
        "short_levels": {"signed": True, "values": [pct, 2 * pct, 3 * pct]},
        "long_tp": {"signed": True, "values": [tp, 0.0]},
        "short_tp": {"signed": True, "values": [-tp, 0.0]},
    }


SPECS = (
    {"code": "CRCNH", "title": "CR × CNH − Si", "root": "CR", "source": "CMEFX", "quote": "RUB", "lot": 1,
     "levels": levels(0.001, 0.00025)},
    {"code": "SI6E", "title": "Si × 6E − Eu", "root": "Si", "source": "CMEFX", "quote": "RUB", "lot": 1,
     "levels": levels(0.001, 0.00025)},
    {"code": "NGCAL", "title": "NG календарь MOEX", "root": "NG", "source": "LISTED", "quote": "USD", "lot": 100,
     "levels": levels(0.02, 0.005)},
)


class Command(BaseCommand):
    help = "Добавляет CR×CNH−Si, Si×6E−Eu, календарь NG и треугольник GD×SI−GLDRUB_TOM."

    def handle(self, *args, **options):
        now = datetime.now(timezone.utc)
        client = None
        if settings.FINAM_API_SECRET:
            from market.finam import FinamClient
            client = FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL)
        try:
            for spec in SPECS:
                pair, created = Pair.objects.update_or_create(code=spec["code"], defaults={
                    "title": spec["title"], "base": "", "future_root": spec["root"],
                    "perp_code": "", "perp_symbol": "", "spot_code": "", "spot_symbol": "",
                    "mic": "RTSX", "foreign_source": spec["source"], "venue": "finam",
                    "quote": spec["quote"], "lot": spec["lot"], "future_scale": 1, "active": True,
                })
                if created:
                    Pair.objects.filter(pk=pair.pk).update(**spec["levels"])
                module = cross if spec["source"] == "CMEFX" else ngcal
                rows = module.ensure(pair, now - timedelta(days=60), now + timedelta(days=400), client=client, update=True)
                source = "даты из Finam" if client else "даты по правилу биржи, без ключа Finam"
                self.stdout.write(f"{pair.code} {'создана' if created else 'обновлена'}: {spec['title']}, окон {len(rows)}, {source}")
                for row in rows[:4]:
                    self.stdout.write(f"  {row['code']} {row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}")
        finally:
            if client is not None:
                client.close()
        self.add_trigds()
        self.stdout.write("Дальше: python manage.py sync_bars, build_spread и перезапуск run_live.")

    def add_trigds(self):
        trigd = Pair.objects.filter(code="TRIGD").first()
        gold = Pair.objects.filter(code="GOLD").first()
        if trigd is None or gold is None:
            self.stdout.write("TRIGDS: нужны пары TRIGD и GOLD, сначала python manage.py add_gold и add_tri")
            return
        pair, created = Pair.objects.update_or_create(code="TRIGDS", defaults={
            "title": "GD + SI − GLDRUB_TOM", "base": "", "future_root": trigd.future_root,
            "perp_code": gold.spot_code, "perp_symbol": gold.spot_symbol,
            "spot_code": trigd.spot_code, "spot_symbol": trigd.spot_symbol,
            "mic": "RTSX", "foreign_source": "TRI", "lot": trigd.lot, "future_scale": 1,
            "quote": "RUB", "active": True,
        })
        if created:
            Pair.objects.filter(pk=pair.pk).update(
                long_levels=trigd.long_levels, short_levels=trigd.short_levels,
                long_tp=trigd.long_tp, short_tp=trigd.short_tp,
            )
        self.stdout.write(f"TRIGDS {'создана' if created else 'обновлена'}: GD + SI − {gold.spot_code}, кварталы GD у TRIGD")
