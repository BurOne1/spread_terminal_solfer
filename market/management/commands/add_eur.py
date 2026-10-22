"""Создаёт пару EUR. Спот — тот же вечный EURRUBF, отдельного TOM нет.

Квартал Eu на Финаме в рублях за 1000 евро, вечный — за 1 евро.
future_scale по умолчанию 1000.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from market.models import Pair

PERP = "EURRUBF@RTSX"


class Command(BaseCommand):
    help = "Добавляет пару евро и заводит её контракты. Спот = вечный."

    def add_arguments(self, parser):
        parser.add_argument("--scale", type=int, default=1000, help="делитель квартального Eu")

    def handle(self, *args, **options):
        pair, created = Pair.objects.update_or_create(
            code="EUR",
            defaults={
                "title": "евро",
                "future_root": "Eu",
                "perp_code": "EURRUBF",
                "perp_symbol": PERP,
                "spot_code": "EURRUBF",
                "spot_symbol": PERP,
                "mic": "RTSX",
                "foreign_source": "ESTR",
                "lot": 1000,
                "future_scale": options["scale"],
                "active": True,
            },
        )
        mark = "создана" if created else "обновлена"
        self.stdout.write(f"пара EUR {mark}: спот = вечный {pair.spot_symbol}, ставка ESTR, scale {pair.future_scale}")
        call_command("ensure_contracts", pair="EUR")

