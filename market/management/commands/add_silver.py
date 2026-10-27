"""Создаёт пару серебра по той же схеме, что золото.

Квартал на Мосбирже — S2 (SL-12.26 = S2Z6), не SLZ6.
Вечный SLVRUBF, спот SLVRUB_TOM. Все в рублях за 1 грамм.
SV — старый долларовый SILV, его не берём.
Очищенный спред как у золота: raw − спот · RUONIA / 100 · t / 365.
Если Finam не отвечает, пара всё равно создаётся с SLVRUBF@RTSX.
"""

from datetime import datetime, timedelta, timezone

import httpx
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from market.finam import FinamClient, FinamError
from market.models import Instrument, Pair

PERP_CANDIDATES = ("SLVRUBF@RTSX", "SLVRUB.P@RTSX", "SLVRUBP@RTSX")
TOM = "SLVRUB_TOM@MISX"


class Command(BaseCommand):
    help = "Добавляет пару серебра SL − SLVRUBF и заводит её контракты."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-probe",
            action="store_true",
            help="не спрашивать Finam, сразу SLVRUBF@RTSX и SLVRUB_TOM@MISX",
        )

    def handle(self, *args, **options):
        if options["no_probe"]:
            perp_symbol = PERP_CANDIDATES[0]
            self.stdout.write(f"проверка Finam пропущена, вечный {perp_symbol}")
            spot_code, spot_symbol = "SLVRUB_TOM", TOM
        else:
            perp_symbol = _first_known(PERP_CANDIDATES, self)
            if perp_symbol is None:
                perp_symbol = PERP_CANDIDATES[0]
                self.stdout.write(f"Finam не ответил, вечный оставлен {perp_symbol}")
            elif perp_symbol == PERP_CANDIDATES[0]:
                self.stdout.write(f"вечный {perp_symbol}")
            else:
                self.stdout.write(f"SLVRUBF на Finam нет, вечный {perp_symbol}")
            spot_code, spot_symbol = "SLVRUB_TOM", TOM
            found = _known(TOM, self)
            if found is False:
                spot_code, spot_symbol = perp_symbol.split("@", 1)[0], perp_symbol
                self.stdout.write("SLVRUB_TOM на Finam нет, спот = вечный")
            elif found is None:
                self.stdout.write("спот оставлен SLVRUB_TOM@MISX")
            else:
                self.stdout.write("SLVRUB_TOM на Finam есть")
        pair, created = Pair.objects.update_or_create(
            code="SILVER",
            defaults={
                "title": "SL − SLVRUB.P",
                "future_root": "S2",
                "perp_code": "SLVRUBF",
                "perp_symbol": perp_symbol,
                "spot_code": spot_code,
                "spot_symbol": spot_symbol,
                "mic": "RTSX",
                "foreign_source": "NONE",
                "lot": 100,
                "future_scale": 1,
                "active": True,
            },
        )
        mark = "создана" if created else "обновлена"
        self.stdout.write(
            f"пара SILVER {mark}: {pair.perp_symbol}, спот {pair.spot_symbol}, "
            "очищенный спред только по RUONIA, лот 100 г"
        )
        call_command("ensure_contracts", pair="SILVER")
        stale = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, code__startswith="SL")
        removed = stale.count()
        if removed:
            stale.delete()
            self.stdout.write(f"убраны старые кварталы SL*: {removed}. Дальше sync_bars --pair SILVER")


def _first_known(symbols, command):
    for symbol in symbols:
        found = _known(symbol, command)
        if found:
            return symbol
        if found is None:
            return None
    return None


def _known(symbol, command):
    if not settings.FINAM_API_SECRET:
        command.stdout.write("FINAM_API_SECRET нет, проверку пропускаю")
        return None
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)
    try:
        with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
            client.bars(symbol, start, end, "TIME_FRAME_H1")
        return True
    except FinamError as exc:
        text = str(exc)
        if exc.status in (400, 404) or "NOT_FOUND" in text or "doesn't exist" in text:
            return False
        command.stderr.write(f"{symbol}: Finam {exc}, беру значение по умолчанию")
        return None
    except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
        command.stderr.write(f"{symbol}: сеть не ответила ({exc.__class__.__name__}), беру значение по умолчанию")
        return None