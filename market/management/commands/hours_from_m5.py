"""Недостающие часовые свечи — из уже загруженных 5-минуток.

Если run_live какое-то время не работал, а 5-минутки потом догрузил sync_m5,
у часов остаётся дыра: часовой график короче, канал на нём другой. Эта команда
собирает часы из 5-минуток (open первой, крайние high/low, close последней,
объём суммой) и пишет только те, которых нет. Часы — только сессии ФОРТС,
как у онлайн-потока. Потом нужен build_spread.

python manage.py hours_from_m5
python manage.py hours_from_m5 --days 60 --pair USD
"""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from market.live import moex_trades
from market.minutes import hours_from_minutes
from market.models import Instrument, MinuteCandle


class Command(BaseCommand):
    help = "Дописывает недостающие часовые свечи ног из 5-минуток (MinuteCandle)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90, help="за сколько последних дней, по умолчанию 90")
        parser.add_argument("--pair", default="", help="только ноги этой пары")

    def handle(self, *args, **options):
        start = datetime.now(timezone.utc) - timedelta(days=options["days"])
        ids = MinuteCandle.objects.filter(ts__gte=start).values_list("instrument_id", flat=True).distinct()
        instruments = Instrument.objects.filter(id__in=list(ids)).select_related("pair").order_by("code")
        if options["pair"]:
            instruments = instruments.filter(pair__code=options["pair"])
        from market.models import Candle
        total = 0
        pairs = set()
        for instrument in instruments:
            before = Candle.objects.filter(instrument=instrument).count()
            first = hours_from_minutes(instrument, start, keep=moex_trades, only_missing=True)
            added = Candle.objects.filter(instrument=instrument).count() - before
            if added:
                total += added
                pairs.add(instrument.pair.code if instrument.pair else "—")
                self.stdout.write(f"  {instrument.code:14} +{added} ч с {first:%Y-%m-%d %H:%M}")
        self.stdout.write(f"итого дописано {total} часов")
        if total:
            self.stdout.write("Дальше: python manage.py build_spread (пары: " + ", ".join(sorted(pairs)) + ")")
