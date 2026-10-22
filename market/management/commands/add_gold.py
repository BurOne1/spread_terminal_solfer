"""Создаёт пару GOLD. Квартал GL, вечный GLDRUBF, спот GLDRUB_TOM.

Все три в рублях за 1 грамм, future_scale = 1.
spread_rate = raw − спот · RUONIA / 100 · t / 365, иностранной ставки нет.
Если Finam не знает GLDRUB_TOM, спот остаётся вечным.
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from market.finam import FinamClient, FinamError
from market.models import Pair

PERP = "GLDRUBF@RTSX"
TOM = "GLDRUB_TOM@MISX"


class Command(BaseCommand):
    help = "Добавляет пару золота и заводит её контракты."

    def handle(self, *args, **options):
        spot_code, spot_symbol = "GLDRUB_TOM", TOM
        found = _tom_exists()
        if found is False:
            spot_code, spot_symbol = "GLDRUBF", PERP
            self.stdout.write("GLDRUB_TOM на Finam нет, спот = вечный GLDRUBF")
        elif found is None:
            self.stdout.write("FINAM_API_SECRET нет, спот оставлен GLDRUB_TOM@MISX")
        else:
            self.stdout.write("GLDRUB_TOM на Finam есть")
        pair, created = Pair.objects.update_or_create(
            code="GOLD",
            defaults={
                "title": "золото",
                "future_root": "GL",
                "perp_code": "GLDRUBF",
                "perp_symbol": PERP,
                "spot_code": spot_code,
                "spot_symbol": spot_symbol,
                "mic": "RTSX",
                "foreign_source": "NONE",
                "lot": 1,
                "future_scale": 1,
                "active": True,
            },
        )
        mark = "создана" if created else "обновлена"
        self.stdout.write(
            f"пара GOLD {mark}: спот {pair.spot_symbol}, ставка только RUONIA, лот {pair.lot} г"
        )
        call_command("ensure_contracts", pair="GOLD")


def _tom_exists():
    if not settings.FINAM_API_SECRET:
        return None
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)
    try:
        with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
            client.bars(TOM, start, end, "TIME_FRAME_H1")
        return True
    except FinamError as exc:
        text = str(exc)
        if exc.status in (400, 404) or "NOT_FOUND" in text or "doesn't exist" in text:
            return False
        raise
