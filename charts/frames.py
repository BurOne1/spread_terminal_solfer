"""Таймфреймы графика и стратегии: 5м, 15м, 1ч, 4ч.

5м — бары SpreadBarM5, 15м — они же, собранные по 15 минут; 1ч — SpreadBar,
4ч — часы, собранные по 4 часа (UTC: 00, 04, 08, …). Свеча старшего таймфрейма —
open первого бара, крайние high/low, close последнего. Если в интервал попала
склейка, берутся только бары нового контракта.

На 5м и 15м окно — не больше MAX_SHORT контрактов: и график, и стратегия.
"""

from collections import namedtuple
from datetime import timezone

from market.models import Candle, MinuteCandle
from strategy.models import SpreadBar, SpreadBarM5
from strategy.params import NO_TRADE_BARS
from strategy.runner import ROW_FIELDS, priced

MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}
SHORT = ("M5", "M15")
MAX_SHORT = 3
Row = namedtuple("Row", ROW_FIELDS)


def frame_of(request):
    value = request.GET.get("tf", "H1")
    return value if value in MINUTES else "H1"


def base(frame):
    """Таймфрейм, из которого собирается frame: M15 из M5, H4 из H1."""
    return "M5" if frame in SHORT else "H1"


def bar_model(frame):
    return SpreadBarM5 if base(frame) == "M5" else SpreadBar


def candle_model(frame):
    return MinuteCandle if base(frame) == "M5" else Candle


def quiet_bars(frame):
    """«Тихая зона» вокруг склейки — те же 26 часов в барах этого таймфрейма."""
    return max(1, round(NO_TRADE_BARS * 60 / MINUTES[frame]))


def short_window(codes):
    """Окно 5м/15м: не больше MAX_SHORT последних контрактов выбранного окна."""
    return codes[-MAX_SHORT:]


def floor(ts, minutes):
    ts = ts.astimezone(timezone.utc)
    total = ts.hour * 60 + ts.minute
    total -= total % minutes
    return ts.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


def rows(pair, frame, codes=None, since=None):
    """Бары пары таймфрейма frame кортежами ROW_FIELDS по времени. codes — только эти контракты, since — не раньше."""
    query = bar_model(frame).objects.filter(pair=pair)
    if codes is not None:
        query = query.filter(contract__code__in=list(codes))
    if since is not None:
        query = query.filter(ts__gte=floor(since, MINUTES[frame]))
    found = list(query.order_by("ts").values_list(*ROW_FIELDS, named=True))
    if frame in ("M15", "H4"):
        return aggregate_rows(found, MINUTES[frame])
    return found


def _groups(items, minutes, ts_of, contract_of):
    """Интервалы: {начало: [элементы]}, у склейки внутри интервала — только новый контракт."""
    groups = {}
    for item in items:
        key = floor(ts_of(item), minutes)
        group = groups.setdefault(key, [])
        if group and contract_of(group[-1]) != contract_of(item):
            group.clear()
        group.append(item)
    return groups


def aggregate_rows(items, minutes):
    out = []
    for key, group in sorted(_groups(items, minutes, lambda row: row.ts, lambda row: row.contract__code).items()):
        first, last = group[0], group[-1]
        out.append(Row(
            ts=key, contract__code=last.contract__code,
            raw_spread=last.raw_spread, spread_rate=last.spread_rate,
            raw_open=first.raw_spread if first.raw_open is None else first.raw_open,
            rate_open=first.spread_rate if first.rate_open is None else first.rate_open,
            days_left=last.days_left, spot=last.spot, fair=last.fair,
            future_close=last.future_close, perp_close=last.perp_close,
        ))
    return out


def aggregate_points(base_points, frame_rows, minutes, basis):
    """Точки графика старшего таймфрейма: значения — по собранным барам, свечи — из свечей базовых точек."""
    from datetime import datetime
    candles = {}
    grouped = _groups(base_points, minutes, lambda point: datetime.fromisoformat(point["t"]), lambda point: point["contract"])
    for key, group in grouped.items():
        parts = [point["candle"] for point in group]
        candles[key] = {"o": parts[0]["o"], "h": max(part["h"] for part in parts),
                        "l": min(part["l"] for part in parts), "c": parts[-1]["c"]}
    points = []
    for row in frame_rows:
        candle = candles.get(row.ts)
        if candle is None:
            continue
        raw, rate = priced(row, basis)
        points.append({
            "t": row.ts.isoformat(), "contract": row.contract__code, "raw": raw, "rate": rate,
            "days": row.days_left, "price": float(row.spot or row.perp_close or 0), "candle": candle,
        })
    return points
