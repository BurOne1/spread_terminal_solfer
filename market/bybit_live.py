"""Живой поток Bybit: догрузка дыр REST-ом, потом kline по WebSocket.

Линейный сокет — вечный и текущий квартал. Спотовый — BTCUSDT.
Пишет те же Candle и пересчитывает только свою пару.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import websockets
from django.db import close_old_connections

from market.bybit import BybitError, funding_history, klines
from market.models import Candle, Instrument, Pair

LINEAR = "wss://stream.bybit.com/v5/public/linear"
SPOT = "wss://stream.bybit.com/v5/public/spot"
STEP = timedelta(hours=1)


def bybit_pairs():
    return list(Pair.objects.filter(active=True, venue="bybit"))


def current_targets(pair):
    now = datetime.now(timezone.utc)
    rows = []
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is not None:
        rows.append((perp, "linear"))
    spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
    if spot is not None and (perp is None or spot.id != perp.id):
        rows.append((spot, "spot"))
    quarter = (
        Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__gt=now)
        .order_by("roll_at")
        .first()
    )
    if quarter is not None:
        rows.append((quarter, "linear"))
    return rows


def save_bars(instrument, bars, pair_code):
    close_old_connections()
    if not bars:
        return "пусто"
    Candle.objects.bulk_create(
        [Candle(instrument=instrument, **bar) for bar in bars],
        update_conflicts=True,
        unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    from strategy.rebuild import rebuild_spread
    count = rebuild_spread(pair_code)
    return f"{instrument.code}: {len(bars)} свечей, баров спреда {count}, последний {bars[-1]['ts']:%Y-%m-%d %H:%M}"


def save_funding(pair, rows):
    close_old_connections()
    from rates.models import FundingPoint
    if not rows:
        return 0
    FundingPoint.objects.bulk_create(
        [FundingPoint(pair=pair, ts=ts, rate=rate, source="bybit") for ts, rate in rows],
        update_conflicts=True,
        unique_fields=["pair", "ts"],
        update_fields=["rate", "source"],
    )
    return len(rows)


def fill_gaps(log, days=30):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    for pair in bybit_pairs():
        for instrument, category in current_targets(pair):
            last = Candle.objects.filter(instrument=instrument).order_by("-ts").values_list("ts", flat=True).first()
            window = start if last is None else max(start, last)
            if now - window <= STEP * 1.5 and last is not None:
                log(f"{instrument.code}: разрывов нет, последняя {last:%Y-%m-%d %H:%M}")
                continue
            log(f"{instrument.code}: догрузка {window:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M}")
            try:
                bars = klines(instrument.finam_symbol, window, now, category=category)
            except BybitError as exc:
                log(f"{instrument.code}: REST {exc}")
                continue
            log(save_bars(instrument, bars, pair.code))
        try:
            rows = funding_history(pair.perp_symbol, start, now)
            got = save_funding(pair, rows)
            log(f"{pair.code}: фандинг {got} точек")
        except BybitError as exc:
            log(f"{pair.code}: фандинг {exc}")


def _bar(item):
    ts = datetime.fromtimestamp(int(item["start"]) / 1000, timezone.utc)
    close = item.get("close")
    return {
        "ts": ts,
        "open": item.get("open", close),
        "high": item.get("high", close),
        "low": item.get("low", close),
        "close": close,
        "volume": item.get("volume", 0),
    }


async def _socket(url, topics, resolver, log):
    async with websockets.connect(url, ping_interval=20, ping_timeout=60) as socket:
        await socket.send(json.dumps({"op": "subscribe", "args": topics}))
        log(f"подписка {url.split('/')[-1]}: " + ", ".join(topics))
        async for raw in socket:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            topic = message.get("topic") or ""
            if not topic.startswith("kline."):
                continue
            symbol = topic.rsplit(".", 1)[-1]
            instrument, pair_code = resolver(symbol, url)
            if instrument is None:
                continue
            bars = []
            for item in message.get("data") or []:
                if not isinstance(item, dict) or item.get("close") in (None, ""):
                    continue
                bar = _bar(item)
                bar["open"] = _dec(bar["open"])
                bar["high"] = _dec(bar["high"])
                bar["low"] = _dec(bar["low"])
                bar["close"] = _dec(bar["close"])
                bar["volume"] = _dec(bar["volume"])
                bars.append(bar)
            if not bars:
                continue
            try:
                detail = await asyncio.to_thread(save_bars, instrument, bars, pair_code)
            except Exception as exc:
                log(f"запись {symbol}: {exc}")
                continue
            log(detail)


def live_targets():
    """Все запросы к базе — в обычном потоке, не из async."""
    rows = []
    for pair in bybit_pairs():
        rows.extend((item, market, pair.code) for item, market in current_targets(pair))
    return rows


def _dec(value):
    from decimal import Decimal
    return Decimal(str(value or 0))
    from decimal import Decimal
    return Decimal(str(value or 0))


async def run_forever(log):
    while True:
        pairs = await asyncio.to_thread(bybit_pairs)
        if not pairs:
            log("пар Bybit нет, жду 30 с")
            await asyncio.sleep(30)
            continue
        try:
            await asyncio.to_thread(fill_gaps, log)
        except Exception as exc:
            log(f"догрузка не удалась: {exc}")
        targets = await asyncio.to_thread(live_targets)
        linear = [f"kline.60.{item.finam_symbol}" for item, market, _code in targets if market == "linear"]
        spot = [f"kline.60.{item.finam_symbol}" for item, market, _code in targets if market == "spot"]

        def resolver(symbol, url):
            market = "spot" if url.endswith("/spot") else "linear"
            for item, item_market, code in targets:
                if item.finam_symbol == symbol and item_market == market:
                    return item, code
            return None, ""

        tasks = []
        if linear:
            tasks.append(asyncio.create_task(_socket(LINEAR, linear, resolver, log)))
        if spot:
            tasks.append(asyncio.create_task(_socket(SPOT, spot, resolver, log)))
        if not tasks:
            log("нечего слушать")
            await asyncio.sleep(30)
            continue
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                log(f"сокет упал: {exc}. повтор через 5 с")
        await asyncio.sleep(5)