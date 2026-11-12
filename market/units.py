"""Масштаб цен инструментов пары: свечи в 1000 раз больше или меньше нужного.

TradingView иногда отдаёт инструмент в другом масштабе: RUS_USDRUBF_1000 —
вечный доллара ×1000 (75970 вместо 75.97). Эталон — последние дни свечей
пары (их пишут Finam и живой поток): медиана цен вечного, спота и кварталов,
кварталы делятся на future_scale пары. Свеча, которая после этого
отличается от эталона в 100–10000 раз, считается в другом масштабе
и приводится делением или умножением на 1000.

Проверяются только пары с вечным (USD, EUR, CNY, металлы): у календарей,
готовых спредов и пар NYMEX цены ног разные по смыслу или бывают отрицательными.
"""

from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.db.models import F, Max

from market.models import Candle, Instrument

RECENT = timedelta(days=7)
KINDS = (Instrument.Kind.PERP, Instrument.Kind.SPOT, Instrument.Kind.FUTURE)


def instrument_scale(instrument):
    """Во сколько раз цена инструмента больше «единичной»: Si — 1000, вечный — 1."""
    pair = instrument.pair
    if pair is not None and instrument.kind == Instrument.Kind.FUTURE:
        return pair.future_scale or 1
    return 1


def checked(pair):
    return pair is not None and bool(pair.perp_code) and not pair.base and pair.foreign_source not in ("TRI", "CAL")


def reference(pair):
    """Эталон пары: медиана единичных цен всех её инструментов за последние RECENT дней."""
    if not checked(pair):
        return None
    instruments = list(Instrument.objects.filter(pair=pair, kind__in=KINDS))
    if not instruments:
        return None
    last = Candle.objects.filter(instrument__in=instruments).aggregate(ts=Max("ts"))["ts"]
    if last is None:
        return None
    values = []
    for instrument in instruments:
        scale = instrument_scale(instrument)
        closes = Candle.objects.filter(instrument=instrument, ts__gte=last - RECENT).values_list("close", flat=True)
        values += [float(close) / scale for close in closes if close and close > 0]
    return median(values) if values else None


def factor(price, ref, scale):
    """Множитель для цены: 0.001, если она ×1000 к эталону, 1000 — если ÷1000, иначе 1."""
    if not ref or price is None or price <= 0:
        return Decimal(1)
    ratio = float(price) / scale / ref
    if 100 < ratio < 10000:
        return Decimal("0.001")
    if 0.0001 < ratio < 0.01:
        return Decimal(1000)
    return Decimal(1)


def bad_candles(instrument, ref):
    """(свечей ×1000, свечей ÷1000) у инструмента относительно эталона."""
    if not ref:
        return 0, 0
    unit = Decimal(str(ref)) * instrument_scale(instrument)
    candles = Candle.objects.filter(instrument=instrument)
    high = candles.filter(close__gt=unit * 100, close__lt=unit * 10000).count()
    low = candles.filter(close__lt=unit / 100, close__gt=unit / 10000).count()
    return high, low


def fix_candles(instrument, ref):
    """Привести свечи инструмента к эталону. Вернуть (поделено, умножено)."""
    if not ref:
        return 0, 0
    unit = Decimal(str(ref)) * instrument_scale(instrument)
    candles = Candle.objects.filter(instrument=instrument)
    down = candles.filter(close__gt=unit * 100, close__lt=unit * 10000)
    up = candles.filter(close__lt=unit / 100, close__gt=unit / 10000)
    result = []
    for rows, mult in ((down, Decimal("0.001")), (up, Decimal(1000))):
        result.append(rows.update(
            open=F("open") * mult, high=F("high") * mult, low=F("low") * mult, close=F("close") * mult,
        ))
    return tuple(result)
