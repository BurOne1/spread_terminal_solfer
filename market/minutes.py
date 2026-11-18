"""5-минутные свечи ног: запись, выбор актуальных инструментов, загрузка истории из Finam.

Finam отдаёт M5 на всю жизнь инструмента, но не больше ~30 дней за запрос
(90 дней — «Invalid date range»), поэтому история грузится окнами по WINDOW.
Актуальные инструменты — фронт и секонд каждой пары, вечный и спот: из них
строятся спреды 5м и 15м. Истёкшие контракты — из TradingView (import_tv).
"""

import time
from datetime import datetime, timedelta, timezone

from django.db.models import Min

from market.finam import FinamError
from market.models import Instrument, MinuteCandle, Pair

WINDOW = timedelta(days=28)
# Пустых окон подряд, после которых история инструмента считается законченной.
EMPTY_STOP = 2
# Вечный и спот без кварталов у пары — не глубже.
LEG_DEPTH = timedelta(days=400)
PAUSE = 0.15
STEP = timedelta(minutes=MinuteCandle.STEP_MINUTES)


def bucket(ts):
    """Начало 5-минутки: 10:07 → 10:05."""
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=ts.minute - ts.minute % MinuteCandle.STEP_MINUTES, second=0, microsecond=0)


def save_minutes(instrument, bars):
    """Бары {ts, open, high, low, close, volume} → MinuteCandle с заменой совпавших. Вернуть число."""
    rows = {}
    for bar in bars:
        rows[bucket(bar["ts"])] = bar
    if not rows:
        return 0
    MinuteCandle.objects.bulk_create(
        [MinuteCandle(instrument=instrument, ts=ts, open=bar["open"], high=bar["high"], low=bar["low"],
                      close=bar["close"], volume=bar.get("volume") or 0) for ts, bar in rows.items()],
        update_conflicts=True, unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"], batch_size=2000,
    )
    return len(rows)


def current_instruments(now=None, pair_code=""):
    """Актуальные ноги Finam: у каждой пары фронт и секонд (свои кварталы, месяцы, ноги
    других бирж, готовые спреды), вечный и спот. Пары на чужих кварталах (календари,
    секонды, TRIGDS) своих инструментов не держат — их ноги приходят от владельцев.

    Вернуть [(инструмент, пара)] без повторов, кварталы раньше вечных: глубина
    вечного и спота считается от самого раннего квартала пары.
    """
    now = now or datetime.now(timezone.utc)
    pairs = Pair.objects.filter(active=True).exclude(venue="bybit").order_by("code")
    if pair_code:
        pairs = pairs.filter(code=pair_code)
    found, legs = {}, {}
    for pair in pairs:
        for kind in (Instrument.Kind.FUTURE, Instrument.Kind.FOREIGN):
            ahead = (Instrument.objects.filter(pair=pair, kind=kind, roll_at__gt=now)
                     .exclude(finam_symbol="").order_by("roll_at")[:2])
            for item in ahead:
                found.setdefault(item.id, (item, pair))
        for code in dict.fromkeys(code for code in (pair.perp_code, pair.spot_code) if code):
            item = Instrument.objects.filter(code=code).exclude(finam_symbol="").first()
            if item is not None:
                legs.setdefault(item.id, (item, pair))
    return list(found.values()) + [row for key, row in legs.items() if key not in found]


def leg_start(pair, now):
    """С какого времени грузить вечный или спот пары: с первой 5-минутки её кварталов."""
    first = (MinuteCandle.objects.filter(instrument__pair=pair, instrument__kind__in=[Instrument.Kind.FUTURE, Instrument.Kind.FOREIGN])
             .aggregate(first=Min("ts"))["first"])
    return first or now - LEG_DEPTH


def load_history(client, instrument, start, end, log=None, sleep=time.sleep):
    """Окна по WINDOW от end назад до start или до EMPTY_STOP пустых окон подряд. Вернуть число свечей."""
    total, empty, cursor = 0, 0, end
    while cursor > start:
        begin = max(start, cursor - WINDOW)
        try:
            bars = client.bars(instrument.finam_symbol, begin, cursor, "TIME_FRAME_M5")
        except FinamError as exc:
            if log:
                log(f"  {instrument.code}: {begin:%Y-%m-%d} → {cursor:%Y-%m-%d}: {str(exc)[:120]}")
            bars = None
        if bars:
            total += save_minutes(instrument, bars)
            empty = 0
        elif bars is not None:
            empty += 1
            if empty >= EMPTY_STOP:
                break
        cursor = begin
        sleep(PAUSE)
    return total


def hours_from_minutes(instrument, start, keep=None, fetched_from=None, only_missing=False):
    """Часовые свечи из 5-минуток с часа start: open первой, крайние high/low, close последней, объём суммой.

    keep(час) — какие часы писать (онлайн — только сессия ФОРТС, как часовой поток).
    fetched_from — с какого момента 5-минутки только что взяты с биржи: час, начатый раньше
    и без своей первой 5-минутки в базе, неполный — его не трогаем (открытие было бы чужим).
    Вернуть первый записанный час или None.
    """
    from market.models import Candle
    first_hour = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = {}
    rows = (MinuteCandle.objects.filter(instrument=instrument, ts__gte=first_hour).order_by("ts")
            .values_list("ts", "open", "high", "low", "close", "volume"))
    for ts, open_, high, low, close, volume in rows:
        hour = ts.replace(minute=0, second=0, microsecond=0)
        if hour not in hours:
            # Неполный час: начат до окна биржи, а его первой 5-минутки в базе нет.
            partial = fetched_from is not None and hour < fetched_from and ts > hour
            hours[hour] = None if partial else [open_, high, low, close, volume or 0]
            continue
        bar = hours[hour]
        if bar is not None:
            bar[1], bar[2], bar[3], bar[4] = max(bar[1], high), min(bar[2], low), close, bar[4] + (volume or 0)
    hours = {hour: bar for hour, bar in hours.items() if bar is not None and (keep is None or keep(hour))}
    if only_missing and hours:
        # Дозаполнить пропуски, уже лежащие часы (Finam, TradingView) не трогать.
        have = set(Candle.objects.filter(instrument=instrument, ts__in=list(hours)).values_list("ts", flat=True))
        hours = {hour: bar for hour, bar in hours.items() if hour not in have}
    if not hours:
        return None
    Candle.objects.bulk_create(
        [Candle(instrument=instrument, ts=hour, open=bar[0], high=bar[1], low=bar[2], close=bar[3], volume=bar[4])
         for hour, bar in hours.items()],
        update_conflicts=True, unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    return min(hours)


def last_minute(instrument):
    return MinuteCandle.objects.filter(instrument=instrument).order_by("-ts").values_list("ts", flat=True).first()
