"""Пересчёт strategy_spreadbar из свечей и ставок, которые уже в базе.

Считает выбранную пару или все активные. Окно квартала — listed_at до roll_at.
"""

from datetime import timezone

from django.db import transaction

from market.models import Candle, Instrument, Pair
from rates.models import RateDay
from strategy.build import build_points, money
from strategy.models import SpreadBar
import threading

_lock = threading.Lock()


def rebuild_spread(pair_code=""):
    pairs = Pair.objects.filter(active=True)
    if pair_code:
        pairs = pairs.filter(code=pair_code)
    total = 0
    for pair in pairs:
        if pair.foreign_source == "TRI":
            total += _rebuild_tri(pair)
        else:
            total += _rebuild_pair(pair)
    return total


def _rebuild_pair(pair):
    scale = pair.future_scale or 1
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None:
        return 0
    perp_by_ts = dict(Candle.objects.filter(instrument=perp).values_list("ts", "close"))
    if not perp_by_ts:
        return 0
    spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
    spot_by_ts = {}
    if spot is not None:
        spot_by_ts = dict(Candle.objects.filter(instrument=spot).values_list("ts", "close"))
    contracts = []
    for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        candles = dict(Candle.objects.filter(instrument=instrument).values_list("ts", "close"))
        if candles:
            contracts.append({
                "id": instrument.id,
                "code": instrument.code,
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "candles": {ts: price / scale for ts, price in candles.items()},
            })
    if not contracts:
        return 0
    if pair.foreign_source == "EWMA14":
        from rates.models import FundingPoint
        funding = list(
            FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
        )
        points = build_points(perp_by_ts, contracts, [], spot_by_ts, funding=funding)
    else:
        if pair.foreign_source == "NONE":
            rates = [(row.date, row.ruonia, 0) for row in RateDay.objects.order_by("date")]
        else:
            field = {"DFEDTARU": "dfedtaru", "ESTR": "estr"}.get(pair.foreign_source, "shibor")
            rates = [
                (row.date, row.ruonia, getattr(row, field))
                for row in RateDay.objects.order_by("date")
            ]
        points = build_points(perp_by_ts, contracts, rates, spot_by_ts)
    by_ts = {}
    for item in points:
        by_ts[item["ts"]] = item
    points = [by_ts[ts] for ts in sorted(by_ts)]
    rows = [
        SpreadBar(
            pair=pair,
            ts=item["ts"],
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
        )
        for item in points
    ]
    with _lock, transaction.atomic():
        SpreadBar.objects.filter(pair=pair).delete()
        SpreadBar.objects.bulk_create(rows, batch_size=500)
    return len(points)


OZ_GRAMS = 31.1034768
TRI_PERP = {"TRIGD": "GOLD", "TRISV": "SILVER"}


def _bars(instrument):
    """Час → close. Наличие бара — час этой ноги."""
    if instrument is None:
        return {}
    return dict(Candle.objects.filter(instrument=instrument).values_list("ts", "close"))


def _common_hour(ts):
    """Пересечение сессий FORTS: будни, 07:00–23:50 МСК = 04:00–20:00 UTC.

    GD, SV, SI и вечные GLDRUBF/SLVRUBF торгуются в одном расписании
    срочного рынка. Бар вне этого окна не общий.
    """
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


def _rebuild_tri(pair):
    """GD+SI−GLDRUBF или SV+SI−SLVRUBF.

    raw и spread_rate пишутся в рублях за грамм: правая шкала золота и
    серебра одна и та же. spot — цена вечного металла в руб/грамм, от неё
    стратегия считает уровни. dollar лежит в foreign_rate.
    Бар только на пересечении часов и внутри общей сессии.
    Склейка — самая ранняя из экспирации металла и склейки SI.
    """
    metal_code = TRI_PERP.get(pair.code)
    metal = Pair.objects.filter(code=metal_code).first() if metal_code else None
    usd = Pair.objects.filter(code="USD").first()
    if metal is None or usd is None:
        return 0
    scale = 1000
    if usd.future_scale and usd.future_scale > 1:
        scale = usd.future_scale
    perp = Instrument.objects.filter(pair=metal, code=metal.perp_code).first()
    perp_bars = _bars(perp)
    if not perp_bars:
        return 0
    si_contracts = []
    for instrument in Instrument.objects.filter(pair=usd, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        bars = _bars(instrument)
        if bars:
            si_contracts.append({
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "bars": {ts: price / scale for ts, price in bars.items()},
            })
    if not si_contracts:
        return 0
    rates = [(row.date, row.ruonia) for row in RateDay.objects.order_by("date")]
    metal_contracts = []
    for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at"):
        if instrument.roll_at is None:
            continue
        bars = _bars(instrument)
        if bars:
            metal_contracts.append({
                "id": instrument.id,
                "listed_at": instrument.listed_at,
                "roll_at": instrument.roll_at,
                "bars": bars,
            })
    if not metal_contracts:
        return 0
    rows = []
    for contract in metal_contracts:
        for ts in sorted(contract["bars"]):
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
            perp_gram = float(perp_rub)
            raw_oz = float(metal_usd) * float(dollar) - perp_gram * OZ_GRAMS
            raw = raw_oz / OZ_GRAMS
            day = ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
            days = max((roll.date() - day).days, 1)
            ruonia = _asof_rate(rates, day)
            fair = perp_gram * (ruonia or 0) / 100.0 * days / 365.0
            rows.append(SpreadBar(
                pair=pair,
                ts=ts,
                contract_id=contract["id"],
                future_close=money(float(metal_usd) * float(dollar) / OZ_GRAMS),
                perp_close=money(perp_gram),
                spot=money(perp_gram),
                raw_spread=money(raw),
                days_left=days,
                ruonia=None if ruonia is None else money(ruonia),
                foreign_rate=money(dollar),
                fair=money(fair),
                spread_rate=money(raw - fair),
            ))
    if not rows:
        return 0
    with _lock, transaction.atomic():
        SpreadBar.objects.filter(pair=pair).delete()
        SpreadBar.objects.bulk_create(rows, batch_size=500)
    return len(rows)


def _asof_rate(rows, day):
    """Последняя известная RUONIA на эту дату или раньше. Пустые дни не обнуляют."""
    from bisect import bisect_right
    dates = [item[0] for item in rows]
    pos = bisect_right(dates, day) - 1
    for index in range(pos, -1, -1):
        value = rows[index][1]
        if value is not None:
            return float(value)
    return None