"""Глубина и размер ответа Finam на мелких таймфреймах (по умолчанию M5).

1) Окно в сутки на разной давности: есть ли там вообще бары — глубина истории.
2) Окна разной длины от сегодня: сколько баров отдаёт один запрос — нужна ли
   загрузка страницами.

python manage.py finam_depth
python manage.py finam_depth --symbols SiZ6@RTSX,CRZ6@RTSX --timeframe TIME_FRAME_M15
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient, FinamError

AGES = (1, 3, 7, 14, 30, 60, 90, 180, 365, 730)
SPANS = (1, 3, 7, 14, 30, 90)
DEFAULT = "SiZ6@RTSX,CRZ6@RTSX,USDRUBF@RTSX,GLDRUB_TOM@MISX"


class Command(BaseCommand):
    help = "Проверяет, на какую глубину и сколько баров за запрос Finam отдаёт на M5 (или другом таймфрейме)."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=DEFAULT, help=f"через запятую, по умолчанию {DEFAULT}")
        parser.add_argument("--timeframe", default="TIME_FRAME_M5")

    def handle(self, *args, **options):
        if not settings.FINAM_API_SECRET:
            raise CommandError("нет FINAM_API_SECRET в .env")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        frame = options["timeframe"]
        symbols = [item.strip() for item in options["symbols"].split(",") if item.strip()]
        with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
            for symbol in symbols:
                self.stdout.write(f"\n{symbol} · {frame}")
                self.stdout.write("  глубина: окно в сутки, N дней назад")
                for age in AGES:
                    end = now - timedelta(days=age - 1)
                    self.stdout.write("    " + self._probe(client, symbol, end - timedelta(days=1), end, frame, f"{age:>4} дн. назад"))
                self.stdout.write("  размер ответа: окно от сегодня назад")
                for span in SPANS:
                    self.stdout.write("    " + self._probe(client, symbol, now - timedelta(days=span), now, frame, f"окно {span:>3} дн."))

    def _probe(self, client, symbol, start, end, frame, label):
        try:
            bars = client.bars(symbol, start, end, frame)
        except FinamError as exc:
            return f"{label}: ошибка {str(exc)[:120]}"
        except Exception as exc:  # сеть, таймаут
            return f"{label}: {type(exc).__name__}"
        if not bars:
            return f"{label}: 0 баров"
        first, last = bars[0]["ts"], bars[-1]["ts"]
        return f"{label}: {len(bars):>5} баров, {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC"
