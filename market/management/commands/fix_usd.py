"""Правит пару доллара: спот Finam и масштаб квартала.

Si котируется за 1000 долларов, вечный и спот — за один.
Спот на Finam — USD000UTSTOM@MISX, не USDRUB_TOM.
"""

from django.core.management.base import BaseCommand

from market.models import Instrument, Pair


class Command(BaseCommand):
    help = "Ставит доллару правильный спот и деление квартала на 1000."

    def handle(self, *args, **options):
        updated = Pair.objects.filter(code="USD").update(
            spot_symbol="USD000UTSTOM@MISX",
            future_scale=1000,
        )
        if not updated:
            self.stderr.write("пары USD нет, сначала создайте её")
            return
        Instrument.objects.filter(code="USDRUB_TOM").update(
            finam_symbol="USD000UTSTOM@MISX",
        )
        pair = Pair.objects.get(code="USD")
        self.stdout.write(
            f"USD: спот {pair.spot_symbol}, квартал делится на {pair.future_scale}"
        )
