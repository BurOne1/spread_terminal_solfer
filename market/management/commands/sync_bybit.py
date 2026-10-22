"""История Bybit: часовые свечи и 8-часовой фандинг вечного."""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError

from market.bybit import BybitError, funding_history, klines
from market.bybit_live import save_bars, save_funding
from market.models import Instrument, Pair


class Command(BaseCommand):
    help = "Качает историю BTC с Bybit в Candle и FundingPoint."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=120, help="сколько дней назад")
        parser.add_argument("--pair", default="BTC")

    def handle(self, *args, **options):
        pair = Pair.objects.filter(code=options["pair"], venue="bybit").first()
        if pair is None:
            raise CommandError("пары Bybit нет, сначала python manage.py add_btc")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=options["days"])
        total = 0
        for instrument, category in _targets(pair, start, end):
            try:
                bars = klines(instrument.finam_symbol, start, end, category=category)
            except BybitError as exc:
                self.stdout.write(f"{instrument.code}: {exc}")
                continue
            detail = save_bars(instrument, bars, pair.code)
            total += len(bars)
            self.stdout.write(detail)
        try:
            rows = funding_history(pair.perp_symbol, start, end)
            got = save_funding(pair, rows)
            self.stdout.write(f"фандинг {got} точек")
        except BybitError as exc:
            self.stdout.write(f"фандинг: {exc}")
        self.stdout.write(f"итого свечей {total}")


def _targets(pair, start, end):
    rows = []
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is not None:
        rows.append((perp, "linear"))
    spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
    if spot is not None:
        rows.append((spot, "spot"))
    quarters = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE)
    for instrument in quarters:
        opened = instrument.listed_at or start
        if instrument.roll_at and opened < end and instrument.roll_at > start:
            rows.append((instrument, "linear"))
    return rows
