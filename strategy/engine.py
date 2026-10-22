"""Проход spread_rate. Уровни — проценты спота этого бара.

Пока позиция открыта кривая двигается от рыночного спреда
квартальный минус вечный и от начисленного фандинга.
fair в прибыль не добавляется: он уже внутри рыночной цены.
"""

from bisect import bisect_right
from datetime import timezone

from strategy.params import (
    COMM_BROKER_KOP,
    COMM_EXCHANGE_PCT,
    MIN_PRICE_SPREAD,
    NO_TRADE_BARS,
    NOTIONAL,
    REQUIRE_LONG_DIFF_SIGN,
    TOUCH_ENTRY,
)

LEVEL_PCTS = (0.01, 0.015, 0.02)
TP_PCTS = (0.0025, 0.0)
FUNDING_AT = (20, 0)  # UTC. Часовой бар 20:00, отдельного 20:50 нет.
BYBIT_HOURS = (0, 8, 16)


def leg_fee(price):
    return price * NOTIONAL * COMM_EXCHANGE_PCT / 100.0 + COMM_BROKER_KOP / 100.0


def levels_of(spot, sign, pcts):
    return tuple(sign * pct * spot for pct in pcts)


def tp_of(spot, sign, pcts):
    return tuple(sign * pct * spot for pct in pcts)


def crossed(prev, now, level, mode="touch"):
    if prev is None or now is None:
        return False
    back = (prev < level <= now) or (prev > level >= now)
    if mode == "reverse":
        return (level < 0 and prev < level <= now) or (level > 0 and prev > level >= now)
    return back


def funding_asof(rows, day):
    dates = [item[0] for item in rows]
    pos = bisect_right(dates, day) - 1
    if pos < 0:
        return 0.0
    return rows[pos][1]


def funding_asof_ts(rows, ts):
    times = [item[0] for item in rows]
    pos = bisect_right(times, ts) - 1
    if pos < 0:
        return 0.0
    return rows[pos][1]


def run(bars, funding, level_pcts=None, tp_pcts=None, entry_mode="touch", average=True, notional=None, funding_mode="daily"):
    """bars: словари ts, contract, raw, rate, spot, fair, future, perp.

    daily: funding это (дата, рубли на контракт), начисление на баре 20:00 UTC.
    interval: funding это (время, ставка в долях). Как на Bybit: только вечный,
    в 00:00, 08:00 и 16:00 UTC, сумма = ставка × цена вечного × размер.
    Лонг спреда — шорт вечного, поэтому положительная ставка приходит в плюс.
    """
    unit = float(NOTIONAL if notional is None else notional)

    def charge(price):
        return price * unit * COMM_EXCHANGE_PCT / 100.0 + COMM_BROKER_KOP / 100.0

    fund_rows = sorted(funding)
    level_pcts = tuple(level_pcts or LEVEL_PCTS)
    tp_pcts = tuple(tp_pcts or TP_PCTS)
    rolls = [i for i in range(1, len(bars)) if bars[i]["contract"] != bars[i - 1]["contract"]]
    close_at = {i - NO_TRADE_BARS for i in rolls if i - NO_TRADE_BARS > 0}
    close_at.add(len(bars) - 1)
    quiet = set()
    for i in rolls:
        quiet.update(range(max(0, i - NO_TRADE_BARS), min(len(bars), i + NO_TRADE_BARS + 1)))

    position = size = 0
    avg = entry_fair = open_fund = realized = contract_base = 0.0
    used, tp_used = set(), set()
    entry_legs = []
    entry_ts = entry_spread = contract = None
    funded_day = None
    funded_stamp = None
    points, trades = [], []

    for i, bar in enumerate(bars):
        force = i in close_at or (i and bar["contract"] != bars[i - 1]["contract"])
        prev = bars[i - 1]["rate"] if i else None
        now, spread = bar["rate"], bar["raw"]
        fair = bar["fair"] if bar["fair"] is not None else 0.0
        spot = bar["spot"] or bar["perp"]

        if position and size:
            tps = tp_of(spot, 1 if position == 1 else -1, tp_pcts)
            hit = [index for index, level in enumerate(tps) if index not in tp_used and crossed(prev, now, level, "touch")]
            if force:
                hit = ["roll"]
            for level in hit:
                close_n = size if force or level == len(tps) - 1 else min(1, size)
                if close_n <= 0:
                    continue
                price = position * (spread - avg) * close_n * unit
                close_fee = close_n * (charge(bar["future"]) + charge(bar["perp"]))
                open_fee = sum(future + perp for future, perp in entry_legs[:close_n])
                fee = close_fee + open_fee
                fund_share = open_fund * close_n / size
                open_fund -= fund_share
                realized += price - close_fee
                notional = close_n * bar["perp"] * unit
                trades.append({
                    "side": "LONG" if position == 1 else "SHORT",
                    "entry": entry_ts,
                    "exit": bar["ts"],
                    "n": close_n,
                    "reason": "roll" if force else "tp",
                    "pnl": price + fund_share - fee,
                    "price": price,
                    "fund": fund_share,
                    "fee": fee,
                    "notional": notional,
                })
                size -= close_n
                entry_legs = entry_legs[close_n:]
                if not force:
                    tp_used.add(level)
                if size == 0:
                    position = avg = entry_fair = open_fund = 0.0
                    used, tp_used, entry_ts = set(), set(), None

        if bar["contract"] != contract:
            contract = bar["contract"]
            contract_base = realized

        different = now and spread and (now > 0) != (spread > 0)
        same = now and spread and (now > 0) == (spread > 0)
        long_ok = (not REQUIRE_LONG_DIFF_SIGN) or different
        wide = abs(spread) >= MIN_PRICE_SPREAD
        blocked = i in quiet

        def enter(side, pct):
            nonlocal position, size, avg, entry_fair, entry_ts, entry_spread, realized
            position = side
            size = 1
            avg = spread
            entry_fair = fair
            entry_ts = bar["ts"]
            entry_spread = spread
            used.add(pct)
            entry_legs.append((charge(bar["future"]), charge(bar["perp"])))
            realized -= charge(bar["future"]) + charge(bar["perp"])

        entries = list(enumerate(level_pcts))
        if not blocked and wide and now is not None and not position:
            for index, pct in entries:
                level = -pct * spot
                if long_ok and crossed(prev, now, level, entry_mode):
                    enter(1, pct)
                    break
            if not position and same:
                for index, pct in entries:
                    level = pct * spot
                    if crossed(prev, now, level, entry_mode):
                        enter(-1, pct)
                        break
        elif average and position and size and not blocked and wide and now is not None:
            sign = 1 if position == 1 else -1
            for index, pct in entries:
                if pct in used or pct <= max(used):
                    continue
                level = sign * pct * spot
                if crossed(prev, now, level, entry_mode) and size < len(entries):
                    avg = (avg * size + spread) / (size + 1)
                    entry_fair = (entry_fair * size + fair) / (size + 1)
                    size += 1
                    used.add(pct)
                    entry_legs.append((charge(bar["future"]), charge(bar["perp"])))
                    realized -= charge(bar["future"]) + charge(bar["perp"])

        if position and size:
            ts = bar["ts"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            day = ts.date()
            if funding_mode == "interval":
                if ts.hour in BYBIT_HOURS and funded_stamp != ts:
                    rate = funding_asof_ts(fund_rows, ts)
                    cash = position * size * unit * float(bar["perp"]) * rate
                    open_fund += cash
                    realized += cash
                    funded_stamp = ts
            else:
                due = ts.hour > FUNDING_AT[0] or (
                    ts.hour == FUNDING_AT[0] and ts.minute >= FUNDING_AT[1]
                )
                if due and funded_day != day:
                    cash = position * size * funding_asof(fund_rows, day)
                    open_fund += cash
                    realized += cash
                    funded_day = day

        mark = 0.0
        if position and size and now is not None:
            mark = position * (spread - avg) * size * unit
        points.append({
            "t": bar["ts"].isoformat(),
            "contract": bar["contract"],
            "equity": realized - contract_base + mark,
            "marker": "entry" if entry_ts == bar["ts"] else "",
        })
        if trades and trades[-1]["exit"] == bar["ts"]:
            points[-1]["marker"] = "exit"

    return points, trades