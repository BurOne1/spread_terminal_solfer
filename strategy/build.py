"""Склейка CR и CNYRUBF в ряд spread_rate. Базу не трогает.

Спот — close CNYRUB_TOM на этот час или последний известный до него.
Если спота ещё нет, берётся close вечного. Дни считаются до roll_at
контракта. Ставка на бар — последняя дата не позже этого бара.
Пропуск одной ставки не обнуляет другую.
"""

from bisect import bisect_right
from datetime import timezone
from decimal import Decimal

from strategy.indicators import ewma_series, fair_from_funding, fair_spread, spread_rate


def stepper(rows, skip_empty=False):
    """Значение на ключ или последнее до него. Ключи собираются один раз, дальше бисекция."""
    if skip_empty:
        rows = [item for item in rows if item[1] is not None]
    keys = [item[0] for item in rows]

    def at(key):
        pos = bisect_right(keys, key) - 1
        return None if pos < 0 else rows[pos][1]
    return at


def build_points(perp_by_ts, contracts, rates, spot_by_ts=None, funding=None, since=None):
    """since — бары раньше не нужны: хвостовой пересчёт их всё равно отбросит."""
    if funding is not None:
        return _funding_points(perp_by_ts, contracts, spot_by_ts, funding, since)
    return _rate_points(perp_by_ts, contracts, rates, spot_by_ts, since)


def _funding_points(perp_by_ts, contracts, spot_by_ts, funding, since=None):
    """BTC: fair = спот · EWMA14(8ч фандинг) · 3 · t. EWMA уже в долях."""
    curve = stepper(ewma_series(sorted(funding)))
    spot_at = stepper(sorted((spot_by_ts or {}).items()))
    points = []
    previous_roll = None
    for contract in contracts:
        roll = contract["roll_at"]
        if roll.tzinfo is None:
            roll = roll.replace(tzinfo=timezone.utc)
        left = contract.get("listed_at")
        if previous_roll is not None:
            left = previous_roll if left is None else max(left, previous_roll)
        if left is not None and left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        for ts in sorted(contract["candles"]):
            if left is not None and ts < left:
                continue
            if ts >= roll or (since is not None and ts < since):
                continue
            perp = perp_by_ts.get(ts)
            if perp is None:
                continue
            future = contract["candles"][ts]
            raw = float(future) - float(perp)
            spot = spot_at(ts)
            if spot is None:
                spot = perp
            bar_day = ts.astimezone(timezone.utc).date()
            days = max((roll.astimezone(timezone.utc).date() - bar_day).days, 1)
            ewma = curve(ts)
            fair = rate_value = None
            if ewma is not None:
                fair = fair_from_funding(spot, ewma, days)
                rate_value = raw - fair
            points.append({
                "ts": ts,
                "contract_id": contract["id"],
                "contract": contract["code"],
                "future_close": future,
                "perp_close": perp,
                "spot": spot,
                "raw_spread": raw,
                "days_left": days,
                "ruonia": None,
                "foreign_rate": ewma,
                "fair": fair,
                "spread_rate": rate_value,
            })
        previous_roll = roll
    points.sort(key=lambda item: item["ts"])
    return points


def _rate_points(perp_by_ts, contracts, rates, spot_by_ts=None, since=None):
    """perp_by_ts: {ts: close}. contracts отсортированы по roll_at и содержат candles.

    rates: (date, ruonia, foreign_rate), по дате.
    spot_by_ts: {ts: close} CNYRUB_TOM. Пустой словарь — вечный вместо спота.
    """
    ruonia_at = stepper([(day, ruonia) for day, ruonia, _foreign in rates], skip_empty=True)
    foreign_at = stepper([(day, foreign) for day, _ruonia, foreign in rates], skip_empty=True)
    spot_at = stepper(sorted((spot_by_ts or {}).items()))
    points = []
    previous_roll = None
    for contract in contracts:
        roll = contract["roll_at"]
        if roll.tzinfo is None:
            roll = roll.replace(tzinfo=timezone.utc)
        left = contract.get("listed_at")
        if previous_roll is not None:
            left = previous_roll if left is None else max(left, previous_roll)
        if left is not None and left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        for ts in sorted(contract["candles"]):
            if left is not None and ts < left:
                continue
            if ts >= roll or (since is not None and ts < since):
                continue
            perp = perp_by_ts.get(ts)
            if perp is None:
                continue
            future = contract["candles"][ts]
            raw = float(future) - float(perp)
            spot = spot_at(ts)
            if spot is None:
                spot = perp
            bar_day = ts.astimezone(timezone.utc).date()
            days = max((roll.astimezone(timezone.utc).date() - bar_day).days, 1)
            ruonia = ruonia_at(bar_day)
            foreign = foreign_at(bar_day)
            fair = rate_value = None
            if ruonia is not None and foreign is not None:
                ruonia = float(ruonia)
                foreign = float(foreign)
                fair = fair_spread(float(spot), ruonia, foreign, days)
                rate_value = spread_rate(raw, float(spot), ruonia, foreign, days)
            points.append({
                "ts": ts,
                "contract_id": contract["id"],
                "contract": contract["code"],
                "future_close": future,
                "perp_close": perp,
                "spot": spot,
                "raw_spread": raw,
                "days_left": days,
                "ruonia": ruonia,
                "foreign_rate": foreign,
                "fair": fair,
                "spread_rate": rate_value,
            })
        previous_roll = roll
    points.sort(key=lambda item: item["ts"])
    return points


def money(value):
    if value is None:
        return None
    return Decimal(f"{value:.6f}")