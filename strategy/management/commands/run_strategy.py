"""Стратегия в своём терминале. Ордеров не шлёт.

Сначала печатает бэктест. Потом слушает новые бары: сделку пишет в базу
и в консоль. График читает точки PNL из базы.
"""

import time

from django.core.management.base import BaseCommand

from market.models import Pair
from strategy.models import PaperTrade, SpreadBar


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
        from strategy.runner import pass_pair
        return pass_pair(pair)

    def _store(self, pair, points):
        from strategy.runner import store_points
        store_points(pair, points)

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