"""Стратегия в своём терминале. Ордеров не шлёт.

Сначала печатает бэктест. Потом слушает новые бары: закрытую сделку пишет
в базу и в консоль, открытую позицию держит в PositionState.
Уровни — сохранённые у пары. Уровни из браузера сюда не попадают.
"""

import time
from decimal import Decimal

from django.core.management.base import BaseCommand

from market.models import Pair
from strategy.models import PaperTrade, PositionState, SpreadBar


class Command(BaseCommand):
    help = "Бэктест в консоль, живые сделки в базу и консоль. Ордеров нет."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="CNY")

    def handle(self, *args, **options):
        pair = Pair.objects.filter(code=options["pair"], active=True).first()
        if pair is None:
            self.stderr.write("нет активной пары")
            return
        _points, trades, opened = self._pass(pair)
        self._report(trades, opened)
        self._keep(pair, opened)
        self.stdout.write("дальше только новые бары. Остановить: Ctrl+C")
        seen = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
        while True:
            last = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
            if last and last != seen:
                seen = last
                _points, trades, opened = self._pass(pair)
                for trade in trades:
                    if trade["exit"] == last:
                        self._save_live(trade)
                        self.stdout.write(self._line(trade) + "  живая")
                if opened and opened["entry"] == last:
                    self.stdout.write(self._open_line(opened) + "  живой вход")
                self._keep(pair, opened)
            time.sleep(15)

    def _pass(self, pair):
        from strategy.runner import pass_pair
        return pass_pair(pair)

    def _keep(self, pair, opened):
        state, _ = PositionState.objects.get_or_create(name=pair.code)
        if opened is None:
            state.side = 0
            state.size = 0
            state.avg_spread = 0
            state.open_fund = 0
            state.contract_code = ""
            state.entry_ts = None
            state.entry_spread = None
            state.entry_legs = []
        else:
            state.side = 1 if opened["side"] == "LONG" else -1
            state.size = opened["n"]
            state.avg_spread = _dec(opened["avg_spread"], 6)
            state.open_fund = _dec(opened["fund"], 4)
            state.contract_code = opened["contract"]
            state.entry_ts = opened["entry"]
            state.entry_spread = _dec(opened["entry_spread"], 6)
            state.entry_legs = [list(leg) for leg in opened["legs"]]
        state.save()

    def _save_live(self, trade):
        notional = trade["notional"] or 0
        PaperTrade.objects.create(
            side=trade["side"],
            entry_ts=trade["entry"],
            exit_ts=trade["exit"],
            n=trade["n"],
            reason=trade["reason"],
            entry_spread=_dec(trade["entry_spread"], 6),
            exit_spread=_dec(trade["exit_spread"], 6),
            exit_future=_dec(trade["exit_future"], 6),
            exit_perp=_dec(trade["exit_perp"], 6),
            price=_dec(trade["price"], 4),
            fund=_dec(trade["fund"], 4),
            fee=_dec(trade["fee"], 4),
            pnl=_dec(trade["pnl"], 4),
            notional=_dec(notional, 4),
            pnl_pct=_dec(100 * trade["pnl"] / notional if notional else 0, 6),
        )

    def _report(self, trades, opened):
        if not trades:
            self.stdout.write("закрытых сделок нет")
        else:
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
        if opened:
            self.stdout.write(self._open_line(opened))

    def _line(self, trade):
        entry = trade["entry"].strftime("%Y-%m-%d %H:%M") if trade["entry"] else "?"
        exit_ = trade["exit"].strftime("%Y-%m-%d %H:%M")
        return f"  {trade['side']:<5} {entry} → {exit_}  {trade['n']} ед.  {trade['reason']}  {trade['pnl']:+,.0f} ₽"

    def _open_line(self, opened):
        entry = opened["entry"].strftime("%Y-%m-%d %H:%M") if opened["entry"] else "?"
        return (
            f"  открыта {opened['side']:<5} с {entry}  {opened['n']} ед.  {opened['contract']}  "
            f"по рынку {opened['price']:+,.0f} ₽, фандинг {opened['fund']:+,.0f} ₽"
        )


def _dec(value, places):
    return Decimal(f"{float(value or 0):.{places}f}")
