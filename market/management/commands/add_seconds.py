"""Календари фронт/секонд и треугольники по золоту на вторых кварталах.

Календарь — секонд минус фронт кварталов базы: SiH2027 − SiZ2026.
В третий четверг после склейки SiZ2026 пара переходит на SiM2027 − SiH2027.
Своих инструментов у этих пар нет: кварталы берутся у базы.

Si, Eu, CR — рубли за единицу валюты, как у базы. GD и SV — доллары за
унцию, PnL у них в долларах. Уровни по умолчанию в десять раз уже, чем
у пар с вечным: календарный спред по споту мал.
"""

from django.core.management.base import BaseCommand

from market.models import Pair

CALENDARS = (
    ("SICAL", "USD", "Si: секонд − фронт"),
    ("EUCAL", "EUR", "Eu: секонд − фронт"),
    ("CRCAL", "CNY", "CR: секонд − фронт"),
    ("GDCAL", "TRIGD", "GD: секонд − фронт"),
    ("SVCAL", "TRISV", "SV: секонд − фронт"),
)
SECONDS = (
    ("TRIGD2", "TRIGD", "GD2 + SI2 − GLDRUBF"),
    ("TRIGL2", "TRIGL", "GD2 + SI2 − GL2"),
)
LEVELS = {
    "long_levels": {"signed": True, "values": [-0.001, -0.0015, -0.002]},
    "short_levels": {"signed": True, "values": [0.001, 0.0015, 0.002]},
    "long_tp": {"signed": True, "values": [0.00025, 0.0]},
    "short_tp": {"signed": True, "values": [-0.00025, 0.0]},
}


class Command(BaseCommand):
    help = "Добавляет календари SI/EU/CR/GD/SV и треугольники TRIGD2, TRIGL2 на секондах."

    def handle(self, *args, **options):
        for code, base_code, title in CALENDARS:
            base = Pair.objects.filter(code=base_code).first()
            if base is None:
                self.stdout.write(f"{code}: нет пары {base_code}, пропуск")
                continue
            dollar = base.foreign_source == "TRI"
            pair, created = Pair.objects.update_or_create(
                code=code,
                defaults={
                    "title": title,
                    "base": base.code,
                    "future_root": base.future_root,
                    "perp_code": "",
                    "perp_symbol": "",
                    "spot_code": "" if dollar else base.spot_code,
                    "spot_symbol": "",
                    "mic": base.mic,
                    "foreign_source": "CAL",
                    "venue": base.venue,
                    "quote": "USD" if dollar else base.quote,
                    "lot": base.lot,
                    "future_scale": base.future_scale,
                    "active": True,
                },
            )
            if created:
                Pair.objects.filter(pk=pair.pk).update(**LEVELS)
            self.stdout.write(f"{code} {'создана' if created else 'обновлена'}: {title}, кварталы {base.code}")
        for code, base_code, title in SECONDS:
            base = Pair.objects.filter(code=base_code).first()
            if base is None:
                self.stdout.write(f"{code}: нет пары {base_code}, сначала python manage.py add_tri")
                continue
            pair, created = Pair.objects.update_or_create(
                code=code,
                defaults={
                    "title": title,
                    "base": base.code,
                    "future_root": base.future_root,
                    "perp_code": base.perp_code,
                    "perp_symbol": base.perp_symbol,
                    "spot_code": base.spot_code,
                    "spot_symbol": base.spot_symbol,
                    "mic": base.mic,
                    "foreign_source": "TRI",
                    "quote": base.quote,
                    "lot": base.lot,
                    "future_scale": base.future_scale,
                    "active": True,
                },
            )
            if created:
                Pair.objects.filter(pk=pair.pk).update(
                    long_levels=base.long_levels, short_levels=base.short_levels,
                    long_tp=base.long_tp, short_tp=base.short_tp,
                )
            self.stdout.write(f"{code} {'создана' if created else 'обновлена'}: {title}")
        self.stdout.write("Дальше: python manage.py sync_bars — догрузит секонды за ~30 дней,")
        self.stdout.write("потом python manage.py build_spread и перезапуск run_live.")
