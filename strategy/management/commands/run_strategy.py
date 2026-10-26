"""Стратегия в своём терминале. Ордеров не шлёт.

Сначала печатает бэктест. Потом слушает новые бары: сделку пишет в базу
и в консоль. График читает точки PNL из базы.
"""

import time
from decimal import Decimal

from django.core.management.base import BaseCommand

from market.models import Pair
from rates.models import FundingDay
from strategy.engine import pair_levels, run
from strategy.models import EquityPoint, PaperTrade, SpreadBar


class Command(BaseCommand):
    help = "Бэктест в консоль, живые сделки в базу и консоль. Ордеров нет."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="CNY")

    def handle(self, *args, **options):
        pair = Pair.objects.filter(code=options["pair"], active=True).first()
        if pair is None:
            self.stderr.write("нет активной пары")
            return
        points, trades = self._pass(pair)
        self._store(pair, points)
        self._report(trades)
        self.stdout.write("дальше только новые бары. Остановить: Ctrl+C")
        seen = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
        while True:
            last = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
            if last and last != seen:
                seen = last
                points, trades = self._pass(pair)
                fresh = [trade for trade in trades if trade["exit"] == last or trade["entry"] == last]
                self._store(pair, points)
                for trade in fresh:
                    self._save_live(trade)
                    self.stdout.write(self._line(trade) + "  живая")
            time.sleep(15)

    def _pass(self, pair):
        bars = [
            {
                "ts": bar.ts,
                "contract": bar.contract.code,
                "raw": float(bar.raw_spread),
                "rate": None if bar.spread_rate is None else float(bar.spread_rate),
                "spot": float(bar.spot),
                "fair": None if bar.fair is None else float(bar.fair),
                "future": float(bar.future_close),
                "perp": float(bar.perp_close),
            }
            for bar in SpreadBar.objects.filter(pair=pair).select_related("contract").order_by("ts")
        ]
        from rates.funding import funding_field
        if pair.foreign_source == "EWMA14":
            from rates.models import FundingPoint
            funding = [
                (ts, float(rate))
                for ts, rate in FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
            ]
            mode = "interval"
        else:
            field = funding_field(pair)
            funding = [
                (day, float(value))
                for day, value in FundingDay.objects.order_by("date").values_list("date", field)
                if value is not None
            ]
            mode = "daily"
        long_pcts, short_pcts, long_tp, short_tp = pair_levels(pair)
        return run(
            bars, funding,
            notional=pair.lot or 1000,
            funding_mode=mode,
            long_pcts=long_pcts,
            short_pcts=short_pcts,
            long_tp=long_tp,
            short_tp=short_tp,
        )

    def _store(self, pair, points):
        EquityPoint.objects.filter(pair=pair).delete()
        EquityPoint.objects.bulk_create([
            EquityPoint(
                pair=pair,
                ts=point["t"],
                contract=point["contract"],
                equity=Decimal(f"{point['equity']:.2f}"),
                marker=point["marker"],
            )
            for point in points
        ], batch_size=1000)

    def _save_live(self, trade):
        PaperTrade.objects.create(
            side=trade["side"],
            entry_ts=trade["entry"],
            exit_ts=trade["exit"],
            n=trade["n"],
            reason=trade["reason"],
            entry_spread=trade.get("entry_spread") or 0,
            exit_spread=0,
            exit_future=0,
            exit_perp=0,
            price=trade["price"],
            fund=trade["fund"],
            fee=trade["fee"],
            pnl=trade["pnl"],
            notional=trade["notional"],
            pnl_pct=0,
        )

    def _report(self, trades):
        if not trades:
            self.stdout.write("сделок нет")
            return
        total = sum(trade["pnl"] for trade in trades)
        wins = sum(1 for trade in trades if trade["pnl"] > 0)
        price = sum(trade["price"] for trade in trades)
        fund = sum(trade["fund"] for trade in trades)
        fee = sum(trade["fee"] for trade in trades)
        self.stdout.write(
            f"закрытий {len(trades)}  winrate {100 * wins / len(trades):.1f}%  итого {total:+,.0f} ₽"
        )
        self.stdout.write(f"спред {price:+,.0f} ₽  фандинг {fund:+,.0f} ₽  комиссия {fee:,.0f} ₽")
        for trade in trades:
            self.stdout.write(self._line(trade))

    def _line(self, trade):
        entry = trade["entry"].strftime("%Y-%m-%d %H:%M") if trade["entry"] else "?"
        exit_ = trade["exit"].strftime("%Y-%m-%d %H:%M")
        return f"  {trade['side']:<5} {entry} → {exit_}  {trade['n']} ед.  {trade['reason']}  {trade['pnl']:+,.0f} ₽"