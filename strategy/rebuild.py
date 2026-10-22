"""Пересчёт strategy_spreadbar из свечей и ставок, которые уже в базе.

Считает выбранную пару или все активные. Окно квартала — listed_at до roll_at.
"""

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