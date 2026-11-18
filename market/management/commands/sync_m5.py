"""5-минутные свечи из Finam по актуальным ногам: фронт, секонд, вечный, спот каждой пары.

Первый запуск — вся история инструмента (Finam хранит её на всю жизнь контракта),
окнами по 28 дней. Повторный — только с последней свечи (минус час: её перепишет бар биржи).
--full — заново всю глубину. Bybit (BTC) сюда не входит.

python manage.py sync_m5
python manage.py sync_m5 --pair USD
python manage.py sync_m5 --full
python manage.py sync_m5 --days 90   (только последние 90 дней)
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient
from market.minutes import current_instruments, last_minute, leg_start, load_history


class Command(BaseCommand):
    help = "Загружает 5-минутные свечи из Finam по фронтам, секондам, вечным и спотам активных пар."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")
        parser.add_argument("--full", action="store_true", help="заново всю историю, а не с последней свечи")
        parser.add_argument("--dry-run", action="store_true", help="только показать список инструментов")
        parser.add_argument("--days", type=int, default=0,
                            help="не глубже N дней назад (на ВМ: например 90); 0 — вся история актуальных контрактов")

    def handle(self, *args, **options):
        now = datetime.now(timezone.utc)
        rows = current_instruments(now, options["pair"])
        if not rows:
            raise CommandError("нет инструментов: проверьте --pair и что у пар заведены контракты")
        self.stdout.write(f"инструментов: {len(rows)}")
        if options["dry_run"]:
            for instrument, pair in rows:
                self.stdout.write(f"  {pair.code:8} {instrument.code:14} {instrument.finam_symbol}")
            return
        if not settings.FINAM_API_SECRET:
            raise CommandError("нет FINAM_API_SECRET в .env")
        total = 0
        with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
            for instrument, pair in rows:
                last = None if options["full"] else last_minute(instrument)
                if last is not None:
                    start = last - timedelta(hours=1)
                elif instrument.kind in ("future", "foreign"):
                    # Квартал или месяц: от начала его торгов, Finam сам скажет, где история кончается.
                    start = (instrument.listed_at or now) - timedelta(days=400)
                else:
                    start = leg_start(pair, now)
                if options["days"]:
                    start = max(start, now - timedelta(days=options["days"]))
                count = load_history(client, instrument, start, now, self.stdout.write)
                total += count
                self.stdout.write(f"  {pair.code:8} {instrument.code:14} {count:>7} свечей с {start:%Y-%m-%d}")
        self.stdout.write(f"итого {total} свечей 5 мин")
