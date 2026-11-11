"""Создаёт пару USD: Si − USDRUBF, спот USD000UTSTOM, ставка ФРС.

Квартал Si на Финаме в рублях за 1000 долларов, вечный и спот — за 1 доллар,
поэтому future_scale 1000. От USD зависят треугольники, SICAL и кросс-пары.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from market.models import Pair


class Command(BaseCommand):
    help = "Добавляет пару доллара и заводит её контракты."

    def handle(self, *args, **options):
        pair, created = Pair.objects.update_or_create(
            code="USD",
            defaults={
                "title": "доллар",
                "future_root": "Si",
                "perp_code": "USDRUBF",
                "perp_symbol": "USDRUBF@RTSX",
                "spot_code": "USDRUB_TOM",
                "spot_symbol": "USD000UTSTOM@MISX",
                "mic": "RTSX",
                "foreign_source": "DFEDTARU",
                "lot": 1000,
                "future_scale": 1000,
                "active": True,
            },
        )
        mark = "создана" if created else "обновлена"
        self.stdout.write(f"пара USD {mark}: Si − USDRUBF, спот {pair.spot_symbol}, ставка ФРС, scale 1000")
        call_command("ensure_contracts", pair="USD", stdout=self.stdout)
