"""Пересчёт strategy_spreadbar из свечей и ставок, которые уже в базе.

Считает выбранную пару или все активные. Окно квартала — listed_at до roll_at.
С since пересчитывается только хвост: бары не раньше since. Свечи читаются
с запасом LOOKBACK назад, чтобы спот и окно контракта были известны.

Живые потоки не пересчитывают на каждый тик: они зовут mark_dirty,
а flush_dirty раз в несколько секунд пересчитывает хвосты отмеченных пар.
"""

import threading
from bisect import bisect_right
from datetime import timedelta, timezone

from django.db import transaction

from market.models import Candle, Instrument, Pair
from rates.models import RateDay
from strategy.build import build_points, money
from strategy.models import SpreadBar

LOOKBACK = timedelta(days=7)

_lock = threading.Lock()
_dirty = {}
_dirty_lock = threading.Lock()


def rebuild_spread(pair_code="", since=None):
    pairs = Pair.objects.filter(active=True)
    if pair_code:
        pairs = pairs.filter(code=pair_code)
    total = 0
    for pair in pairs:
        if pair.foreign_source == "TRI":
            total += _rebuild_tri(pair, since)
        else:
            total += _rebuild_pair(pair, since)
    return total


# Треугольник собирается из чужих ног: свечи этих пар меняют и его.
TRI_DEPS = {
    "TRIGD": ("TRIGD", "GOLD", "USD"),
    "TRISV": ("TRISV", "SILVER", "USD"),
    "TRIGL": ("TRIGD", "GOLD", "USD"),
}


def mark_dirty(pair_code, since):
    """Запомнить, что у пары поменялись свечи начиная с since."""
    if not pair_code:
        return
    with _dirty_lock:
        old = _dirty.get(pair_code)
        _dirty[pair_code] = since if old is None or since < old else old


def flush_dirty():
    """Пересчитать хвосты отмеченных пар и их треугольников. Вернуть {код: баров}."""
    with _dirty_lock:
        batch = dict(_dirty)
        _dirty.clear()
    for code, since in list(batch.items()):
        for tri, deps in TRI_DEPS.items():
            if code in deps:
                batch[tri] = min(batch.get(tri, since), since)
    done = {}
    for code, since in sorted(batch.items()):
        try:
            done[code] = rebuild_spread(code, since=since)
        except Exception:
            # Непересчитанные пары остаются отмеченными до следующего раза.
            for left, left_since in batch.items():
                if left not in done:
                    mark_dirty(left, left_since)
            raise
    return done


def _load_from(since):
    return None if since is None else since - LOOKBACK


def _ohlc(instrument, load_from):
    """Час → (open, close). Наличие бара — час этой ноги."""
    if instrument is None:
        return {}
    rows = Candle.objects.filter(instrument=instrument)
    if load_from is not None:
        rows = rows.filter(ts__gte=load_from)
    return {ts: (open_, close) for ts, open_, close in rows.values_list("ts", "open", "close")}


def _side(rows, index, scale=1):
    return {ts: prices[index] / scale for ts, prices in rows.items()}


def _rebuild_pair(pair, since=None):
    scale = pair.future_scale or 1
    load_from = _load_from(since)
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None:
        return 0
    perp_rows = _ohlc(perp, load_from)
    if not perp_rows:
        return 0
    spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
    spot_rows = _ohlc(spot, load_from) if spot is not None else {}
    futures = []
    for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        if since is not None and instrument.roll_at <= load_from:
            continue
        candles = _ohlc(instrument, load_from)
        if candles:
            futures.append((instrument, candles))
    if not futures:
        return 0
    funding = None
    rates = []
    if pair.foreign_source == "EWMA14":
        from rates.models import FundingPoint
        funding = list(
            FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
        )
    elif pair.foreign_source == "NONE":
        rates = [(row.date, row.ruonia, 0) for row in RateDay.objects.order_by("date")]
    else:
        field = {"DFEDTARU": "dfedtaru", "ESTR": "estr"}.get(pair.foreign_source, "shibor")
        rates = [
            (row.date, row.ruonia, getattr(row, field))
            for row in RateDay.objects.order_by("date")
        ]

    def points_at(index):
        contracts = [
            {
                "id": instrument.id,
                "code": instrument.code,
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "candles": _side(candles, index, scale),
            }
            for instrument, candles in futures
        ]
        points = build_points(_side(perp_rows, index), contracts, rates, _side(spot_rows, index), funding=funding)
        return {item["ts"]: item for item in points}

    closes = points_at(1)
    opens = points_at(0)
    rows = []
    for ts in sorted(closes):
        if since is not None and ts < since:
            continue
        item = closes[ts]
        opened = opens.get(ts) or {}
        rows.append(SpreadBar(
            pair=pair,
            ts=ts,
            contract_id=item["contract_id"],
            future_close=item["future_close"],
            perp_close=item["perp_close"],
            spot=item["spot"],
            raw_spread=money(item["raw_spread"]),
            days_left=item["days_left"],
            ruonia=money(item["ruonia"]),
            foreign_rate=money(item["foreign_rate"]),
            fair=money(item["fair"]),
            spread_rate=money(item["spread_rate"]),
            raw_open=money(opened.get("raw_spread")),
            rate_open=money(opened.get("spread_rate")),
        ))
    return _store(pair, rows, since)


OZ_GRAMS = 31.1034768
TRI_PERP = {"TRIGD": "GOLD", "TRISV": "SILVER", "TRIGL": "GOLD"}


def _common_hour(ts):
    """Пересечение сессий FORTS: будни, 07:00–23:50 МСК = 04:00–20:00 UTC."""
    ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if ts.weekday() >= 5:
        return False
    return 4 <= ts.hour <= 20


def _window_price(contracts, ts):
    for contract in contracts:
        left = contract["listed_at"]
        if left is not None and ts < left:
            continue
        if ts >= contract["roll_at"]:
            continue
        price = contract["bars"].get(ts)
        if price is not None:
            return price, contract
    return None, None


def _days(roll, ts):
    day = ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
    return max((roll.date() - day).days, 1), day


def _rates():
    return [
        (row.date, row.ruonia, row.dfedtaru)
        for row in RateDay.objects.order_by("date")
    ]


def _si_contracts(usd, load_from):
    scale = usd.future_scale if usd.future_scale and usd.future_scale > 1 else 1000
    rows = []
    for instrument in Instrument.objects.filter(pair=usd, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        bars = _ohlc(instrument, load_from)
        if bars:
            rows.append({
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "bars": {ts: (open_ / scale, close / scale) for ts, (open_, close) in bars.items()},
            })
    return rows


def _future_contracts(pair, load_from):
    rows = []
    for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        bars = _ohlc(instrument, load_from)
        if bars:
            rows.append({
                "id": instrument.id,
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "bars": bars,
            })
    return rows


def _tri_values(metal_usd, dollar, perp_rub, ruonia, fed, days):
    """raw, fair и очищенный спред треугольника в рублях за грамм."""
    perp_gram = float(perp_rub)
    raw = (float(metal_usd) * float(dollar) - perp_gram * OZ_GRAMS) / OZ_GRAMS
    # Ставку ФРС можно убрать: у синтетики металл_USD × SI долларовая
    # ставка сокращается, и кэрри остаётся ≈ RUONIA. Оставлена намеренно.
    fair = perp_gram * ((ruonia or 0) + (fed or 0)) / 100.0 * days / 365.0
    return raw, fair, raw - fair


def _tri_row(pair, ts, contract_id, roll, rates, metal_usd, dollar, perp_rub):
    """SpreadBar треугольника. Цены ног — пары (open, close)."""
    days = _days(roll, ts)[0]
    day = ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
    ruonia, fed = _asof_rates(rates, day)
    raw, fair, cleaned = _tri_values(metal_usd[1], dollar[1], perp_rub[1], ruonia, fed, days)
    raw_open, _fair_open, cleaned_open = _tri_values(metal_usd[0], dollar[0], perp_rub[0], ruonia, fed, days)
    return SpreadBar(
        pair=pair,
        ts=ts,
        contract_id=contract_id,
        future_close=money(float(metal_usd[1]) * float(dollar[1]) / OZ_GRAMS),
        perp_close=money(float(perp_rub[1])),
        spot=money(float(perp_rub[1])),
        raw_spread=money(raw),
        days_left=days,
        ruonia=None if ruonia is None else money(ruonia),
        foreign_rate=money(dollar[1]),
        fair=money(fair),
        spread_rate=money(cleaned),
        raw_open=money(raw_open),
        rate_open=money(cleaned_open),
    )


def _rebuild_tri(pair, since=None):
    """GD+SI−GLDRUBF, SV+SI−SLVRUBF или GD+SI−GL.

    SI — на (RUONIA − ставка ФРС) и свой срок, металл — на ставку ФРС,
    рублёвая нога — на RUONIA. raw остаётся грязным. Склейка — самая ранняя из ног.
    foreign_rate у треугольника хранит курс SI, а не ставку.
    """
    if pair.code == "TRIGL":
        return _rebuild_trigl(pair, since)
    load_from = _load_from(since)
    metal = Pair.objects.filter(code=TRI_PERP.get(pair.code, "")).first()
    usd = Pair.objects.filter(code="USD").first()
    if metal is None or usd is None:
        return 0
    perp = Instrument.objects.filter(pair=metal, code=metal.perp_code).first()
    perp_bars = _ohlc(perp, load_from)
    si_contracts = _si_contracts(usd, load_from)
    metal_contracts = _future_contracts(pair, load_from)
    if not perp_bars or not si_contracts or not metal_contracts:
        return 0
    rates = _rates()
    rows = []
    for contract in metal_contracts:
        for ts in sorted(contract["bars"]):
            if since is not None and ts < since:
                continue
            if not _common_hour(ts):
                continue
            left = contract["listed_at"]
            if left is not None and ts < left:
                continue
            if ts >= contract["roll_at"]:
                continue
            metal_usd = contract["bars"].get(ts)
            perp_rub = perp_bars.get(ts)
            dollar, si = _window_price(si_contracts, ts)
            if metal_usd is None or perp_rub is None or dollar is None:
                continue
            roll = contract["roll_at"]
            if si is not None and si["roll_at"] < roll:
                roll = si["roll_at"]
            rows.append(_tri_row(pair, ts, contract["id"], roll, rates, metal_usd, dollar, perp_rub))
    return _store(pair, rows, since)


def _rebuild_trigl(pair, since=None):
    """GD+SI−GL. Все три ноги квартальные и уже в базе."""
    load_from = _load_from(since)
    gold = Pair.objects.filter(code="GOLD").first()
    gd = Pair.objects.filter(code="TRIGD").first()
    usd = Pair.objects.filter(code="USD").first()
    if gold is None or gd is None or usd is None:
        return 0
    gl_contracts = _future_contracts(gold, load_from)
    gd_contracts = _future_contracts(gd, load_from)
    si_contracts = _si_contracts(usd, load_from)
    if not gl_contracts or not gd_contracts or not si_contracts:
        return 0
    rates = _rates()
    rows = []
    for gl in gl_contracts:
        for ts in sorted(gl["bars"]):
            if since is not None and ts < since:
                continue
            if not _common_hour(ts):
                continue
            left = gl["listed_at"]
            if left is not None and ts < left:
                continue
            if ts >= gl["roll_at"]:
                continue
            gl_rub = gl["bars"].get(ts)
            gd_usd, gd_contract = _window_price(gd_contracts, ts)
            dollar, si = _window_price(si_contracts, ts)
            if gl_rub is None or gd_usd is None or dollar is None:
                continue
            roll = gl["roll_at"]
            if gd_contract is not None and gd_contract["roll_at"] < roll:
                roll = gd_contract["roll_at"]
            if si is not None and si["roll_at"] < roll:
                roll = si["roll_at"]
            rows.append(_tri_row(pair, ts, gl["id"], roll, rates, gd_usd, dollar, gl_rub))
    return _store(pair, rows, since)


def _store(pair, rows, since=None):
    """Без since — заменить ряд пары целиком, с since — только хвост."""
    if not rows and since is None:
        return 0
    by_ts = {}
    for row in rows:
        by_ts[row.ts] = row
    rows = [by_ts[ts] for ts in sorted(by_ts)]
    with _lock, transaction.atomic():
        stale = SpreadBar.objects.filter(pair=pair)
        if since is not None:
            stale = stale.filter(ts__gte=since)
        stale.delete()
        SpreadBar.objects.bulk_create(rows, batch_size=500)
    return len(rows)


def _asof_rates(rows, day):
    """Последние известные RUONIA и ставка ФРС на эту дату или раньше."""
    dates = [item[0] for item in rows]
    pos = bisect_right(dates, day) - 1
    ruonia = fed = None
    for index in range(pos, -1, -1):
        if ruonia is None and rows[index][1] is not None:
            ruonia = float(rows[index][1])
        if fed is None and rows[index][2] is not None:
            fed = float(rows[index][2])
        if ruonia is not None and fed is not None:
            break
    return ruonia, fed
