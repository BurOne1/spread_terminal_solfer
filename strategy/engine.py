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


def _stored(raw):
    if isinstance(raw, dict) and raw.get("signed"):
        return [float(item) for item in raw.get("values", [])], True
    if isinstance(raw, (list, tuple)):
        return [float(item) for item in raw], False
    return [], False


def pair_levels(pair):
    """Доли спота со знаком: лонг, шорт, тейки лонга, тейки шорта.

    Старый формат — два одинаковых списка положительных процентов.
    Тогда лонг был зеркалом со знаком минус, шорт со знаком плюс.
    Новый формат помечен signed и хранит знак как есть.
    """
    if pair is None:
        return (
            [-item for item in LEVEL_PCTS],
            list(LEVEL_PCTS),
            list(TP_PCTS),
            [-item if item else 0.0 for item in TP_PCTS],
        )
    longs, long_signed = _stored(pair.long_levels)
    shorts, short_signed = _stored(pair.short_levels)
    long_tp, ltp_signed = _stored(pair.long_tp)
    short_tp, stp_signed = _stored(pair.short_tp)
    signed = long_signed or short_signed or ltp_signed or stp_signed
    if not signed and not longs and not shorts:
        longs = [-item for item in LEVEL_PCTS]
        shorts = list(LEVEL_PCTS)
    elif not signed and longs == shorts and bool(longs) and all(item >= 0 for item in longs):
        shorts = list(longs)
        longs = [-item for item in longs]
    if not signed and not long_tp and not short_tp:
        long_tp = list(TP_PCTS)
        short_tp = [-item if item else 0.0 for item in TP_PCTS]
    elif not signed and long_tp == short_tp and bool(long_tp) and all(item >= 0 for item in long_tp):
        short_tp = [-item if item else 0.0 for item in long_tp]
    return longs, shorts, long_tp, short_tp


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


def _deeper(pct, taken):
    """Следующий вход той же стороны только дальше от нуля, чем уже взятые."""
    if not taken:
        return True
    return abs(pct) > max(abs(item) for item in taken)


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


def run(bars, funding, level_pcts=None, tp_pcts=None, entry_mode="touch", average=True, notional=None, funding_mode="daily", long_pcts=None, short_pcts=None, long_tp=None, short_tp=None):
    """bars: словари ts, contract, raw, rate, spot, fair, future, perp.

    Уровни — доли спота со знаком. Лонг и шорт задаются отдельно,
    тейки тоже: один и тот же знак у обеих сторон допустим.
    Шорт входит по своему уровню без проверки знака спреда.
    Лонг по-прежнему только при разном знаке spread_rate и сырого спреда.
    """
    unit = float(NOTIONAL if notional is None else notional)

    def charge(price):
        return price * unit * COMM_EXCHANGE_PCT / 100.0 + COMM_BROKER_KOP / 100.0

    fund_rows = sorted(funding)
    if long_pcts is None:
        base = tuple(float(item) for item in (level_pcts or LEVEL_PCTS))
        long_pcts = tuple(-abs(item) for item in base)
        short_pcts = tuple(abs(item) for item in base)
    else:
        long_pcts = tuple(float(item) for item in long_pcts)
        short_pcts = tuple(float(item) for item in (short_pcts or []))
    if long_tp is None:
        base = tuple(float(item) for item in (tp_pcts or TP_PCTS))
        long_tp = tuple(abs(item) if item else 0.0 for item in base)
        short_tp = tuple(-abs(item) if item else 0.0 for item in base)
    else:
        long_tp = tuple(float(item) for item in long_tp)
        short_tp = tuple(float(item) for item in (short_tp or []))
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
            tps = long_tp if position == 1 else short_tp
            hit = [index for index, pct in enumerate(tps) if index not in tp_used and crossed(prev, now, pct * spot, "touch")]
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
        long_ok = (not REQUIRE_LONG_DIFF_SIGN) or different
        wide = abs(spread) >= MIN_PRICE_SPREAD
        blocked = i in quiet

        def enter(side, key):
            nonlocal position, size, avg, entry_fair, entry_ts, entry_spread, realized
            position = side
            size = 1
            avg = spread
            entry_fair = fair
            entry_ts = bar["ts"]
            entry_spread = spread
            used.add(key)
            entry_legs.append((charge(bar["future"]), charge(bar["perp"])))
            realized -= charge(bar["future"]) + charge(bar["perp"])

        if not blocked and wide and now is not None and not position:
            for index, pct in enumerate(long_pcts):
                if long_ok and crossed(prev, now, pct * spot, entry_mode):
                    enter(1, ("L", index))
                    break
            if not position:
                for index, pct in enumerate(short_pcts):
                    if crossed(prev, now, pct * spot, entry_mode):
                        enter(-1, ("S", index))
                        break
        elif average and position and size and not blocked and wide and now is not None:
            ladder = long_pcts if position == 1 else short_pcts
            tag = "L" if position == 1 else "S"
            taken = [ladder[index] for side, index in used if side == tag and index < len(ladder)]
            for index, pct in enumerate(ladder):
                if (tag, index) in used or not _deeper(pct, taken):
                    continue
                if crossed(prev, now, pct * spot, entry_mode) and size < len(ladder):
                    avg = (avg * size + spread) / (size + 1)
                    entry_fair = (entry_fair * size + fair) / (size + 1)
                    size += 1
                    used.add((tag, index))
                    taken.append(pct)
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
        held = "L" if position == 1 else "S" if position == -1 else ""
        if not held and trades and trades[-1]["exit"] == bar["ts"]:
            held = "L" if trades[-1]["side"] == "LONG" else "S"
        event = "entry" if entry_ts == bar["ts"] else ""
        if trades and trades[-1]["exit"] == bar["ts"]:
            event = "exit"
        points.append({
            "t": bar["ts"].isoformat(),
            "contract": bar["contract"],
            "equity": realized - contract_base + mark,
            "marker": held + event,
        })

    return points, trades