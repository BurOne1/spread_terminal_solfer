"""Треугольные пары: GD+SI−GLDRUBF и SV+SI−SLVRUBF.

GD и SV — долларовые кварталы (унция). SI берётся из пары USD,
вечный рублёвый — из GOLD или SILVER. Свои инструменты только у GD и SV.

raw на унцию = металл_USD × (SI/1000) − вечный_RUB × 31.1034768
очищенный = raw − вечный_на_унцию × RUONIA/100 × t/365
Объём SI в долларах равен долларовому объёму GD или SV.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from market.models import Pair

SPECS = (
    {
        "code": "TRIGD",
        "title": "GD + SI − GLDRUBF",
        "future_root": "GD",
        "perp_code": "GLDRUBF",
        "perp_symbol": "GLDRUBF@RTSX",
        "metal_pair": "GOLD",
        "oz": 1,
    },
    {
        "code": "TRISV",
        "title": "SV + SI − SLVRUBF",
        "future_root": "SV",
        "perp_code": "SLVRUBF",
        "perp_symbol": "SLVRUBF@RTSX",
        "metal_pair": "SILVER",
        "oz": 10,
    },
)


class Command(BaseCommand):
    help = "Добавляет треугольные пары GD+SI−GLDRUBF и SV+SI−SLVRUBF."

    def handle(self, *args, **options):
        usd = Pair.objects.filter(code="USD").first()
        if usd is None:
            raise CommandError("нужна пара USD: из неё берётся SI")
        for spec in SPECS:
            metal = Pair.objects.filter(code=spec["metal_pair"]).first()
            if metal is None:
                raise CommandError(f"нужна пара {spec['metal_pair']}: из неё берётся вечный")
            pair, created = Pair.objects.update_or_create(
                code=spec["code"],
                defaults={
                    "title": spec["title"],
                    "future_root": spec["future_root"],
                    "perp_code": spec["perp_code"],
                    "perp_symbol": spec["perp_symbol"],
                    "spot_code": "SI",
                    "spot_symbol": usd.perp_symbol or "USDRUBF@RTSX",
                    "mic": "RTSX",
                    "foreign_source": "TRI",
                    "lot": spec["oz"],
                    "future_scale": 1,
                    "quote": "RUB",
                    "active": True,
                },
            )
            mark = "создана" if created else "обновлена"
            self.stdout.write(
                f"пара {pair.code} {mark}: {spec['title']}, "
                f"{spec['oz']} унц. в контракте, SI и вечный из {spec['metal_pair']}/USD"
            )
            call_command("ensure_contracts", pair=pair.code)
        self.stdout.write("Дальше: python manage.py sync_bars --pair TRIGD")
        self.stdout.write("и python manage.py sync_bars --pair TRISV")
        self.stdout.write("потом python manage.py build_spread --pair TRIGD")
        self.stdout.write("и python manage.py build_spread --pair TRISV")
