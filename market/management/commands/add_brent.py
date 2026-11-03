"""Пара BRNYMEX: месячный BR Мосбиржи минус BZ NYMEX, доллары за баррель.

Обе ноги рассчитываются по одной цене ICE Brent, справедливый спред — ноль.
Лот пары — 1000 баррелей: один BZ против 100 BR, PnL в долларах на эту связку.
Уровни — доли цены BZ: 0,1 % при $70 — это 7 центов.
"""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from market.brent import ensure_window, windows
from market.models import Pair

CODE = "BRNYMEX"
LEVELS = {
    "long_levels": {"signed": True, "values": [-0.001, -0.002, -0.003]},
    "short_levels": {"signed": True, "values": [0.001, 0.002, 0.003]},
    "long_tp": {"signed": True, "values": [0.00025, 0.0]},
    "short_tp": {"signed": True, "values": [-0.00025, 0.0]},
}


class Command(BaseCommand):
    help = "Добавляет пару BRNYMEX (BR Мосбиржи − BZ NYMEX) и её месячные контракты."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=14, help="на сколько месяцев вперёд завести контракты")

    def handle(self, *args, **options):
        pair, created = Pair.objects.update_or_create(
            code=CODE,
            defaults={
                "title": "BR MOEX − BZ NYMEX",
                "base": "",
                "future_root": "BR",
                "perp_code": "",
                "perp_symbol": "",
                "spot_code": "",
                "spot_symbol": "",
                "mic": "RTSX",
                "foreign_source": "NYMEX",
                "venue": "finam",
                "quote": "USD",
                "lot": 1000,
                "future_scale": 1,
                "active": True,
            },
        )
        if created:
            Pair.objects.filter(pk=pair.pk).update(**LEVELS)
        self.stdout.write(f"{CODE} {'создана' if created else 'обновлена'}")
        now = datetime.now(timezone.utc)
        for row in windows(now - timedelta(days=60), now + timedelta(days=31 * options["months"])):
            _instrument, made = ensure_window(pair, row, update=True)
            mark = "новый" if made else "окно"
            self.stdout.write(
                f"  {mark} {row['code']} / {row['nymex_code']} "
                f"{row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}"
            )
        self.stdout.write("Дальше: python manage.py sync_bars --pair BRNYMEX, build_spread и перезапуск run_live.")
