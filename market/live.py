"""Живой поток Finam: сначала догрузка разрывов, потом WebSocket.

При старте и после обрыва сокета смотрим последнюю свечу в базе и добираем
REST-ом всё от часа перед ней до текущего времени. Дальше держим wss://api.finam.ru/ws и пишем каждый пришедший бар.
JWT живёт около 15 минут, поэтому раз в 11 минут сокет переоткрывается.

Таймфрейм только H1: часовые свечи, расписание и спред считаются по часу.
Пока час идёт, свечу строят котировки. При каждом переоткрытии сокета
последний час берётся REST-ом заново, и свечу заменяет бар биржи.

На каждый тик база не трогается. Котировки копятся в памяти свечой
текущего часа, бары сокета — последним снимком по символу. Раз в
FLUSH_SECONDS всё пишется одной пачкой, и пересчитываются хвосты
задетых пар. В лог идут новые часы и ошибки, не каждая котировка.
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import websockets
from django.conf import settings
from django.db import close_old_connections

from market.finam import PERP_SYMBOL, SPOT_SYMBOL, FinamClient, FinamError, future_symbol, parse_decimal
from market.models import Candle, Instrument, Pair
from market.roll import quarter_points

WS_URL = "wss://api.finam.ru/ws"
JWT_REFRESH = 11 * 60
FLUSH_SECONDS = 5
# Сводку пересчёта печатать не чаще, чем раз в столько секунд.
LOG_EVERY = 60
# Котировка старше этого — снимок при подписке (после ночи, выходных, праздника).
STALE_QUOTE = timedelta(hours=1)
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


def quarter_symbols(owner, now, count):
    """Символы ближайших count кварталов, что ещё не склеились: фронт, секонд.

    Недостающий квартал заводится в базе, чтобы после склейки секонд
    следующего окна уже был, даже если ensure_contracts давно не запускали.
    """
    if owner is None:
        return []
    rows = [
        row for row in quarter_points(owner.future_root, now - timedelta(days=1), now + timedelta(days=400))
        if row["roll_at"] > now
    ][:count]
    for row in rows:
        Instrument.objects.get_or_create(
            code=row["code"],
            defaults={
                "pair": owner,
                "kind": Instrument.Kind.FUTURE,
                "finam_symbol": future_symbol(row["secid"]),
                "lot": owner.lot,
                "listed_at": row["listed_at"],
                "roll_at": row["roll_at"],
            },
        )
    return [f"{row['secid']}@{owner.mic}" for row in rows]


def live_symbols():
    """Символы сокета. Символ пересчитывается при каждом переоткрытии, раз в 11 минут:
    после склейки фронт и секонд сами сдвигаются на следующие кварталы.
    """
    now = datetime.now(timezone.utc)
    pairs = {pair.code: pair for pair in Pair.objects.filter(active=True).exclude(venue="bybit")}

    def owner(code):
        return pairs.get(code) or Pair.objects.filter(code=code).first()

    symbols = []
    for pair in pairs.values():
        if pair.calendar:
            symbols.extend(quarter_symbols(owner(pair.base), now, 2))
            continue
        if pair.foreign_source == "TRI":
            # Вечный и SI уже подписаны парами GOLD/SILVER и USD.
            if pair.base:
                legs = ["TRIGD", "USD", "GOLD"] if pair.tri_code == "TRIGL" else [pair.tri_code, "USD"]
                for code in legs:
                    symbols.extend(quarter_symbols(owner(code), now, 2)[1:])
            elif pair.code != "TRIGL":
                symbols.extend(quarter_symbols(pair, now, 1))
            continue
        symbols.extend([pair.perp_symbol, pair.spot_symbol])
        symbols.extend(quarter_symbols(pair, now, 1))
    if not symbols:
        symbols = [PERP_SYMBOL, SPOT_SYMBOL]
    return list(dict.fromkeys(symbol for symbol in symbols if symbol))


def find_instrument(symbol):
    if not symbol:
        return None
    found = Instrument.objects.select_related("pair")
    instrument = found.filter(finam_symbol__iexact=symbol).first()
    if instrument is not None:
        return instrument
    code = symbol.split("@", 1)[0]
    return found.filter(code__iexact=code).first()


def _mark(instrument, since):
    from strategy.rebuild import mark_dirty
    if instrument.pair is not None:
        mark_dirty(instrument.pair.code, since)


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


# Часовой бар подписан временем открытия.
# 04:00 UTC = 07:00 МСК, начало утренней сессии срочного рынка.
# 20:00 UTC ещё содержит торги до 23:50 МСК. 21:00 UTC — уже клиринг.
MOEX_FIRST_HOUR = 4
MOEX_LAST_HOUR = 20


def moex_trades(ts):
    ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if ts.weekday() >= 5:
        return False
    return MOEX_FIRST_HOUR <= ts.hour <= MOEX_LAST_HOUR


def market_open(now=None):
    return moex_trades(now or datetime.now(timezone.utc))


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


# Свеча текущего часа из котировок: (id инструмента, час) → [open, high, low, close].
_book = {}
_book_pairs = {}
_book_dirty = set()
# Бары сокета: символ → {час: бар}, последний снимок до записи.
_socket_bars = {}
_book_lock = threading.Lock()


def book_quote(instrument, ts, price):
    """Котировка в свечу часа в памяти. Пишет flush_book, не этот вызов.

    Возвращает текст для лога или None, если это обычный тик внутри часа.
    """
    now = datetime.now(timezone.utc)
    if not market_open(now):
        return f"биржа закрыта, {hour_floor(now):%Y-%m-%d %H:%M} не пишу"
    if now - ts > STALE_QUOTE:
        return f"старая котировка {ts:%Y-%m-%d %H:%M}, не пишу"
    # Тик, пришедший уже в новом часу, открывает новую свечу: так бар
    # появляется сразу с наступлением часа, а не с первой сделкой.
    hour = hour_floor(now)
    key = (instrument.id, hour)
    with _book_lock:
        row = _book.get(key)
        if row is None:
            _book[key] = [price, price, price, price]
            if instrument.pair is not None:
                _book_pairs[instrument.id] = instrument.pair.code
        else:
            row[1] = max(row[1], price)
            row[2] = min(row[2], price)
            row[3] = price
        _book_dirty.add(key)
    if row is None:
        return f"новый час {hour:%Y-%m-%d %H:%M} close={price}"
    return None


def save_quote(symbol, ts, price):
    """Котировка по символу. Для сокета символы разрешаются заранее, см. _socket."""
    close_old_connections()
    instrument = find_instrument(symbol)
    if instrument is None:
        return f"нет инструмента для {symbol or '?'}"
    return book_quote(instrument, ts, price) or f"обновлён {hour_floor(datetime.now(timezone.utc)):%Y-%m-%d %H:%M}"


def book_bars(symbol, bars):
    with _book_lock:
        _socket_bars.setdefault(symbol, {}).update({bar["ts"]: bar for bar in bars})


def flush_book(log=None):
    """Бары сокета и свечи котировок — в базу пачкой. Вернуть число записанных свечей.

    Бар сокета пишется как есть. Свеча котировок сливается с тем, что
    уже в базе: open и объём — базы (их ставит бар биржи), high и low —
    крайние из двух, close — последняя котировка.
    """
    with _book_lock:
        socket_bars = dict(_socket_bars)
        _socket_bars.clear()
        keys = list(_book_dirty)
        _book_dirty.clear()
        quotes = {key: tuple(_book[key]) for key in keys}
        pair_of = dict(_book_pairs)
        current = hour_floor(datetime.now(timezone.utc))
        for key in [key for key in _book if key[1] < current]:
            del _book[key]
    if not socket_bars and not quotes:
        return 0
    close_old_connections()
    written = 0
    for symbol, rows in socket_bars.items():
        count, detail = save_bars(symbol, [rows[ts] for ts in sorted(rows)])
        written += count
        if log is not None and count and not detail.startswith("новых 0,"):
            log(f"{symbol}: {detail}")
    if not quotes:
        return written
    ids = {instrument_id for instrument_id, _hour in quotes}
    hours = {hour for _instrument_id, hour in quotes}
    existing = {
        (row.instrument_id, row.ts): row
        for row in Candle.objects.filter(instrument_id__in=ids, ts__in=hours)
    }
    candles = []
    for (instrument_id, hour), (open_, high, low, close) in quotes.items():
        old = existing.get((instrument_id, hour))
        if old is not None:
            open_, high, low = old.open, max(high, old.high), min(low, old.low)
        candles.append(Candle(
            instrument_id=instrument_id, ts=hour, open=open_, high=high, low=low, close=close,
            volume=old.volume if old is not None else 0,
        ))
    Candle.objects.bulk_create(
        candles,
        update_conflicts=True,
        unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    from strategy.rebuild import mark_dirty
    for instrument_id, hour in quotes:
        mark_dirty(pair_of.get(instrument_id, ""), hour)
    return written + len(candles)


def save_bars(symbol, bars):
    close_old_connections()
    instrument = find_instrument(symbol)
    if instrument is None:
        return 0, f"нет инструмента для {symbol or '?'}"
    tradable = [bar for bar in bars if moex_trades(bar["ts"])]
    existing = set(
        Candle.objects.filter(instrument=instrument, ts__in=[bar["ts"] for bar in tradable]).values_list("ts", flat=True)
    ) if tradable else set()
    if not market_open():
        tradable = [bar for bar in tradable if bar["ts"] not in existing]
        if not tradable:
            return 0, "биржа закрыта, старые бары не трогаю"
    if not tradable:
        return 0, "вне торговых часов, не пишу"
    Candle.objects.bulk_create(
        [Candle(instrument=instrument, **bar) for bar in tradable],
        update_conflicts=True,
        unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    _mark(instrument, min(bar["ts"] for bar in tradable))
    fresh = sum(bar["ts"] not in existing for bar in tradable)
    return len(tradable), (
        f"новых {fresh}, обновлено {len(tradable) - fresh}, "
        f"последний {tradable[-1]['ts']:%Y-%m-%d %H:%M}"
    )


def gap_windows(times, step, now, depth):
    """Только хвост: от часа перед последней свечой до сейчас.

    Последние свечи берутся заново всегда: пока час шёл, её строили
    котировки, а теперь её заменит бар биржи. Внутренние пропуски
    добирает sync_bars, не каждый запуск сокета.
    """
    if not times:
        return [(now - depth, now)]
    return [(times[-1] - step, now)]


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
            for start, end in gap_windows(times, step, now, depth):
                log(f"{symbol}: догрузка {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}")
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


def _resolve(symbols):
    close_old_connections()
    return {symbol: find_instrument(symbol) for symbol in symbols}


async def _socket(symbols, frame, token, log):
    # Инструменты разрешаются один раз на сессию: на тик базы нет.
    known = await asyncio.to_thread(_resolve, symbols)

    async def instrument_of(symbol):
        if symbol not in known:
            known.update(await asyncio.to_thread(_resolve, [symbol]))
        return known[symbol]

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
                    instrument = await instrument_of(symbol)
                    if instrument is None:
                        continue
                    detail = book_quote(instrument, quote["ts"], quote["price"])
                    if detail and detail.startswith("новый час"):
                        log(f"{symbol}: {detail}")
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
            if await instrument_of(symbol) is None:
                log(f"нет инструмента для {symbol or '?'}")
                continue
            book_bars(symbol, bars)


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


async def _flush(log):
    """Раз в FLUSH_SECONDS: свечи из памяти в базу, потом хвосты задетых пар."""
    pending = {}
    logged = time.monotonic()
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        try:
            done = await asyncio.to_thread(_flush_now, log)
        except Exception as exc:
            log(f"пересчёт спреда не удался: {exc}")
            continue
        for code, count in done.items():
            pending[code] = pending.get(code, 0) + count
        if pending and time.monotonic() - logged >= LOG_EVERY:
            log("пересчёт спреда за минуту: " + ", ".join(f"{code} {count}" for code, count in sorted(pending.items())))
            pending = {}
            logged = time.monotonic()


def _flush_now(log=None):
    close_old_connections()
    from strategy.rebuild import flush_dirty
    flush_book(log)
    return flush_dirty()


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
    flush = asyncio.create_task(_flush(log))
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
                while True:
                    try:
                        token = client.token()
                        break
                    except Exception as exc:
                        log(f"JWT не получен: {exc}. повтор через 15 с")
                        await asyncio.sleep(15)
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
        flush.cancel()
        # Последние секунды котировок не теряются при остановке.
        try:
            await asyncio.to_thread(_flush_now, log)
        except Exception as exc:
            log(f"последняя запись не удалась: {exc}")