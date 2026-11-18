"""Пары CRCNH, SI6E, NGCAL, TRIGDS и TRISVS.

CRCNH  CR × CNH × 1000 − Si   рубли за 1000 долларов (пункты Si), CNH — CME
SI6E   Si × 6E − Eu           рубли за 1000 евро (пункты Eu), 6E — CME
NGCAL  NGX6NGZ6@RTSX          календарь газа Мосбиржи одним инструментом, $/MMBtu
NGNYMEX NG MOEX − NG NYMEX     NGZ6 ↔ NGF27: NYMEX месяцем позже, $/MMBtu
TRIGDS GD × SI − GLDRUB_TOM   как TRIGD, но третья нога — спот золота, без фандинга
TRISVS SV × SI − SLVRUB_TOM   как TRISV, но третья нога — спот серебра, без фандинга

Кросс-пары и NGCAL — справедливый ноль. Уровни кросс-пар — доли цены Si или Eu,
у NGCAL — абсолютные, в $/MMBtu.
Лот кросс-пары — один контракт Si или Eu: PnL в рублях на него. Лот NGCAL —
100 MMBtu, один календарь. TRIGDS берёт уровни TRIGD, TRISVS — TRISV.
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from market import cross, ngcal, ngnymex
from market.models import Instrument, Pair


SILVER_SPOT = "SLVRUB_TOM"
SILVER_SPOT_SYMBOL = "SLVRUB_TOM@MISX"


def levels(pct, tp):
    return {
        "long_levels": {"signed": True, "values": [-pct]},
        "short_levels": {"signed": True, "values": [pct]},
        "long_tp": {"signed": True, "values": [tp]},
        "short_tp": {"signed": True, "values": [-tp]},
    }


SPECS = (
    {"code": "CRCNH", "title": "CR × CNH − Si", "root": "CR", "source": "CMEFX", "quote": "RUB", "lot": 1,
     "levels": levels(0.001, 0.00025)},
    {"code": "SI6E", "title": "Si × 6E − Eu", "root": "Si", "source": "CMEFX", "quote": "RUB", "lot": 1,
     "levels": levels(0.001, 0.00025)},
    # Уровни NGCAL абсолютные, $/MMBtu: спред торгуется одной котировкой, цены ног нет.
    {"code": "NGCAL", "title": "NG календарь MOEX", "root": "NG", "source": "LISTED", "quote": "USD", "lot": 100,
     "levels": levels(0.07, 0.02)},
    {"code": "NGNYMEX", "title": "NG MOEX − NG NYMEX", "root": "NG", "source": "NGNYM", "quote": "USD", "lot": 100,
     "levels": levels(0.003, 0.001)},
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
                module = {"CMEFX": cross, "LISTED": ngcal, "NGNYM": ngnymex}[spec["source"]]
                rows = module.ensure(pair, now - timedelta(days=60), now + timedelta(days=400), client=client, update=True)
                if spec["source"] != "CMEFX":
                    source = "склейка — последний рабочий день месяца MOEX"
                else:
                    source = "даты из Finam" if client else "даты по правилу биржи, без ключа Finam"
                self.stdout.write(f"{pair.code} {'создана' if created else 'обновлена'}: {spec['title']}, окон {len(rows)}, {source}")
                for row in rows[:4]:
                    self.stdout.write(f"  {row['code']} {row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}")
        finally:
            if client is not None:
                client.close()
        self.add_trigds()
        self.add_trisvs()
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

    def add_trisvs(self):
        """TRISV на споте серебра: SV + SI − SLVRUB_TOM, без фандинга. Кварталы SV — у TRISV."""
        trisv = Pair.objects.filter(code="TRISV").first()
        silver = Pair.objects.filter(code="SILVER").first()
        if trisv is None or silver is None:
            self.stdout.write("TRISVS: нужны пары TRISV и SILVER, сначала python manage.py add_silver и add_tri")
            return
        if silver.spot_code != SILVER_SPOT:
            # Спот серебра на Финаме — SLVRUB_TOM@MISX (рубли за грамм, как вечный SLVRUBF).
            Pair.objects.filter(pk=silver.pk).update(spot_code=SILVER_SPOT, spot_symbol=SILVER_SPOT_SYMBOL)
            silver.refresh_from_db()
            self.stdout.write(f"SILVER: спот теперь {SILVER_SPOT_SYMBOL} (был вечный), нужен sync_bars --pair SILVER")
        Instrument.objects.update_or_create(code=SILVER_SPOT, defaults={
            "pair": silver, "kind": Instrument.Kind.SPOT, "finam_symbol": SILVER_SPOT_SYMBOL, "lot": silver.lot,
        })
        pair, created = Pair.objects.update_or_create(code="TRISVS", defaults={
            "title": "SV + SI − SLVRUB_TOM", "base": "", "future_root": trisv.future_root,
            "perp_code": silver.spot_code, "perp_symbol": silver.spot_symbol,
            "spot_code": trisv.spot_code, "spot_symbol": trisv.spot_symbol,
            "mic": "RTSX", "foreign_source": "TRI", "lot": trisv.lot, "future_scale": 1,
            "quote": "RUB", "active": True,
        })
        if created:
            # Новая пара сразу видна группе «Просмотр»; потом галочку правят в админке.
            Pair.objects.filter(pk=pair.pk).update(
                long_levels=trisv.long_levels, short_levels=trisv.short_levels,
                long_tp=trisv.long_tp, short_tp=trisv.short_tp, viewer=True,
            )
        self.stdout.write(f"TRISVS {'создана' if created else 'обновлена'}: SV + SI − {SILVER_SPOT}, кварталы SV у TRISV")
