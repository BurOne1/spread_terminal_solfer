"""Живой поток Finam: сначала догрузка разрывов, потом WebSocket.

Таймфрейм берётся из BAR_TIMEFRAME, поэтому M1/M5/H1 меняются одной переменной.
При старте и после обрыва сокета смотрим последние свечи в базе. Если между ними
или до текущего времени дыра больше полутора баров — добираем её REST-ом.
Дальше держим wss://api.finam.ru/ws и пишем каждый пришедший бар.
JWT живёт около 15 минут, поэтому раз в 11 минут сокет переоткрывается.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import websockets
from django.conf import settings
from django.db import close_old_connections

from market.finam import PERP_SYMBOL, SPOT_SYMBOL, FinamClient, FinamError, parse_decimal
from market.models import Candle, Instrument, Pair
from market.roll import quarter_points

WS_URL = "wss://api.finam.ru/ws"
JWT_REFRESH = 11 * 60
BAR_STEP = {
    "TIME_FRAME_M1": timedelta(minutes=1),
    "TIME_FRAME_M5": timedelta(minutes=5),
    "TIME_FRAME_M15": timedelta(minutes=15),
    "TIME_FRAME_M30": timedelta(minutes=30),
    "TIME_FRAME_H1": timedelta(hours=1),
    "TIME_FRAME_H2": timedelta(hours=2),
    "TIME_FRAME_H4": timedelta(hours=4),
    "TIME_FRAME_H8": timedelta(hours=8),
    "TIME_FRAME_D": timedelta(days=1),
}
BAR_DEPTH = {
    "TIME_FRAME_M1": timedelta(days=7),
    "TIME_FRAME_D": timedelta(days=365),
}


def timeframe():
    return getattr(settings, "BAR_TIMEFRAME", "TIME_FRAME_H1")


def step_of(frame):
    return BAR_STEP.get(frame, timedelta(hours=1))


def depth_of(frame):
    return BAR_DEPTH.get(frame, timedelta(days=30))


def live_symbols():
    now = datetime.now(timezone.utc)
    symbols = []
    for pair in Pair.objects.filter(active=True).exclude(venue="bybit"):
        symbols.extend([pair.perp_symbol, pair.spot_symbol])
        for row in quarter_points(pair.future_root, now - timedelta(days=1), now + timedelta(days=1)):
            if row["roll_at"] > now:
                symbols.append(f"{row['secid']}@{pair.mic}")
                break
    if not symbols:
        symbols = [PERP_SYMBOL, SPOT_SYMBOL]
    return list(dict.fromkeys(symbols))


def find_instrument(symbol):
    if not symbol:
        return None
    instrument = Instrument.objects.filter(finam_symbol__iexact=symbol).first()
    if instrument is not None:
        return instrument
    code = symbol.split("@", 1)[0]
    return Instrument.objects.filter(code__iexact=code).first()


def parse_any_timestamp(value):
    if isinstance(value, dict):
        seconds = value.get("seconds") or value.get("sec")
        if seconds is not None:
            return datetime.fromtimestamp(int(seconds), timezone.utc)
        value = value.get("value") or value.get("time")
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def bar_from_node(node):
    if not isinstance(node, dict):
        return None
    ts = None
    for key in ("timestamp", "time", "ts", "datetime", "date"):
        if key in node:
            ts = parse_any_timestamp(node[key])
            if ts is not None:
                break
    close = node.get("close", node.get("c"))
    if ts is None or close in (None, ""):
        return None
    return {
        "ts": ts,
        "open": parse_decimal(node.get("open", node.get("o", close))),
        "high": parse_decimal(node.get("high", node.get("h", close))),
        "low": parse_decimal(node.get("low", node.get("l", close))),
        "close": parse_decimal(close),
        "volume": parse_decimal(node.get("volume", node.get("v", 0))),
    }


def walk_bars(node, found):
    if isinstance(node, dict):
        bar = bar_from_node(node)
        if bar is not None:
            found.append(bar)
        for value in node.values():
            walk_bars(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_bars(item, found)


def extract_bars(message):
    if isinstance(message, dict) and isinstance(message.get("payload"), str):
        try:
            message = {**message, "payload": json.loads(message["payload"])}
        except json.JSONDecodeError:
            pass
    found = []
    walk_bars(message, found)
    rows = {}
    for bar in found:
        rows[bar["ts"]] = bar
    return [rows[key] for key in sorted(rows)]


def message_symbol(message):
    if not isinstance(message, dict):
        return ""
    payload = message.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    for source in (payload, data, message):
        for key in ("symbol", "ticker", "security"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    key = message.get("subscription_key")
    if isinstance(key, str) and ":" in key:
        return key.split(":", 1)[0]
    return ""


def hour_floor(ts):
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=0, second=0, microsecond=0)


def extract_quotes(message):
    if isinstance(message, dict) and isinstance(message.get("payload"), str):
        try:
            message = {**message, "payload": json.loads(message["payload"])}
        except json.JSONDecodeError:
            return []
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
    quotes = payload.get("quote") or payload.get("quotes") or []
    if isinstance(quotes, dict):
        quotes = [quotes]
    rows = []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        price = quote.get("last") or quote.get("close")
        ts = parse_any_timestamp(quote.get("timestamp"))
        symbol = quote.get("symbol") or ""
        if price in (None, "") or ts is None:
            continue
        rows.append({"symbol": symbol, "ts": ts, "price": parse_decimal(price)})
    return rows


def save_quote(symbol, ts, price):
    close_old_connections()
    instrument = find_instrument(symbol)
    if instrument is None:
        return f"нет инструмента для {symbol or '?'}"
    hour = hour_floor(ts)
    now_hour = hour_floor(datetime.now(timezone.utc))
    if now_hour > hour:
        hour = now_hour
    candle = Candle.objects.filter(instrument=instrument, ts=hour).first()
    created = candle is None
    if created:
        Candle.objects.create(
            instrument=instrument, ts=hour,
            open=price, high=price, low=price, close=price, volume=0,
        )
    else:
        candle.high = max(candle.high, price)
        candle.low = min(candle.low, price)
        candle.close = price
        candle.save(update_fields=["high", "low", "close"])
    from strategy.rebuild import rebuild_spread
    spread_count = rebuild_spread()
    mark = "новый час" if created else "обновлён"
    return f"{mark} {hour:%Y-%m-%d %H:%M} close={price} баров спреда {spread_count}"


def save_bars(symbol, bars):
    close_old_connections()
    instrument = find_instrument(symbol)
    if instrument is None:
        return 0, f"нет инструмента для {symbol or '?'}"
    existing = set(
        Candle.objects.filter(instrument=instrument, ts__in=[bar["ts"] for bar in bars]).values_list("ts", flat=True)
    )
    Candle.objects.bulk_create(
        [Candle(instrument=instrument, **bar) for bar in bars],
        update_conflicts=True,
        unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    from strategy.rebuild import rebuild_spread
    spread_count = rebuild_spread()
    fresh = sum(bar["ts"] not in existing for bar in bars)
    return len(bars), (
        f"новых {fresh}, обновлено {len(bars) - fresh}, баров спреда {spread_count}, "
        f"последний {bars[-1]['ts']:%Y-%m-%d %H:%M}"
    )


def gap_windows(times, step, now, depth):
    """Только хвост: от последней свечи до сейчас.

    Ночь и выходные длиннее полутора баров, но это не дыра. Внутренние
    пропуски добирает sync_bars, не каждый запуск сокета.
    """
    if not times:
        return [(now - depth, now)]
    if now - times[-1] > step * 1.5:
        return [(times[-1], now)]
    return []


def fill_gaps(symbols, frame, log):
    step = step_of(frame)
    depth = depth_of(frame)
    now = datetime.now(timezone.utc)
    with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
        for symbol in symbols:
            close_old_connections()
            instrument = find_instrument(symbol)
            if instrument is None:
                log(f"{symbol}: нет в базе, пропуск")
                continue
            times = list(
                Candle.objects.filter(instrument=instrument, ts__gte=now - depth)
                .order_by("ts")
                .values_list("ts", flat=True)
            )
            windows = gap_windows(times, step, now, depth)
            if not windows:
                last = times[-1].astimezone(timezone.utc)
                log(f"{symbol}: разрывов нет, последняя свеча {last:%Y-%m-%d %H:%M}")
                continue
            for start, end in windows:
                log(f"{symbol}: разрыв {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}")
                try:
                    bars = client.bars(symbol, start, end, frame)
                except FinamError as exc:
                    log(f"{symbol}: REST {exc}")
                    continue
                if not bars:
                    log(f"{symbol}: REST пусто")
                    continue
                _, detail = save_bars(symbol, bars)
                log(f"{symbol}: {detail}")


async def _socket(symbols, frame, token, log):
    headers = {"Authorization": f"Bearer {token}"}
    async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=20, ping_timeout=60) as socket:
        for symbol in symbols:
            await socket.send(json.dumps({
                "action": "SUBSCRIBE",
                "type": "BARS",
                "data": {"symbol": symbol, "timeframe": frame},
                "token": token,
            }))
            log(f"подписка баров {symbol} {frame}")
        await socket.send(json.dumps({
            "action": "SUBSCRIBE",
            "type": "QUOTES",
            "data": {"symbols": symbols},
            "token": token,
        }))
        log("подписка котировок: " + ", ".join(symbols))
        async for raw in socket:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log("сокет не JSON: " + text[:200])
                continue
            kind = message.get("type")
            if kind == "ERROR":
                log(f"Finam ошибка: {message.get('error_info') or message}")
                continue
            if kind == "EVENT":
                log(f"Finam событие: {message.get('event_info')}")
                continue
            quotes = extract_quotes(message)
            if quotes:
                for quote in quotes:
                    symbol = quote["symbol"] or message_symbol(message)
                    try:
                        detail = await asyncio.to_thread(save_quote, symbol, quote["ts"], quote["price"])
                    except Exception as exc:
                        log(f"котировка не записалась {symbol or '?'}: {exc}")
                        continue
                    log(f"{symbol or '?'}: {detail}")
                continue
            if message.get("subscription_type") == "QUOTES":
                continue
            bars = extract_bars(message)
            symbol = message_symbol(message)
            if not bars:
                if message.get("subscription_type") != "QUOTES":
                    log("сокет без баров: " + text[:400].replace("\n", " "))
                continue
            if len(bars) > 2:
                log(f"{symbol or '?'}: снимок {len(bars)} баров, пишу последний")
                bars = bars[-1:]
            if not symbol and len(symbols) == 1:
                symbol = symbols[0]
            try:
                _, detail = await asyncio.to_thread(save_bars, symbol, bars)
            except Exception as exc:
                log(f"запись не удалась {symbol or '?'}: {exc}")
                continue
            log(f"записано {symbol or '?'}: {detail}")


async def _rates(log):
    while True:
        try:
            result = await asyncio.to_thread(_sync_recent)
            ruonia = result["ruonia"]
            shibor = result["shibor"]
            ruonia_text = "нет" if ruonia is None else f"{ruonia[0]} = {ruonia[1]}"
            shibor_text = "нет" if shibor is None else f"{shibor[0]} = {shibor[1]}"
            estr = result.get("estr")
            estr_text = "нет" if estr is None else f"{estr[0]} = {estr[1]}"
            log(
                f"ставки за 14 дней: RUONIA {result['ruonia_days']} дней, последняя {ruonia_text}; "
                f"SHIBOR {result['shibor_days']} дней, последняя {shibor_text}; "
                f"DFEDTARU {result.get('dfedtaru_days', 0)} дней; "
                f"ESTR {result.get('estr_days', 0)} дней, последняя {estr_text}"
            )
            if result.get("note"):
                log(result["note"])
        except Exception as exc:
            log(f"ставки не обновились: {exc}")
        await asyncio.sleep(24 * 60 * 60)


async def _funding(log):
    from datetime import datetime, timedelta, timezone

    while True:
        try:
            result = await asyncio.to_thread(_fill_funding)
            last = result["last"]
            text = "нет" if last is None else f"{last[0]} = {last[1]} руб/контракт"
            log(f"фандинг {result['from']} → {result['to']}: {result['days']} дней, последний {text}")
        except Exception as exc:
            log(f"фандинг не обновился: {exc}")
        now = datetime.now(timezone.utc)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())


def _sync_recent():
    from rates.sync import sync_recent
    return sync_recent()


def _fill_funding():
    from rates.funding import fill_funding
    return fill_funding()


async def run_forever(log):
    frame = timeframe()
    log(f"таймфрейм {frame}, шаг {step_of(frame)}")
    rates = asyncio.create_task(_rates(log))
    funding = asyncio.create_task(_funding(log))
    try:
        while True:
            symbols = await asyncio.to_thread(live_symbols)
            log("символы: " + ", ".join(symbols))
            try:
                await asyncio.to_thread(fill_gaps, symbols, frame, log)
            except Exception as exc:
                log(f"проверка разрывов не удалась: {exc}")
            client = FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL)
            try:
                token = client.token()
            finally:
                client.close()
            try:
                await asyncio.wait_for(_socket(symbols, frame, token, log), timeout=JWT_REFRESH)
                log("сокет закрылся, переподключение")
            except asyncio.TimeoutError:
                log("обновляю JWT и проверяю разрывы")
            except Exception as exc:
                log(f"сокет упал: {exc}. повтор через 5 с")
                await asyncio.sleep(5)
    finally:
        rates.cancel()
        funding.cancel()