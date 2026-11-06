"""run_radar: стакан и лента Finam по списку символов, AlgoPack, отметки жизни.

Символы: ноги активных спредов (как у run_live, с переходом после склейки)
и инструменты радара из админки. Раз в 11 минут сокет переоткрывается с
новым JWT, и список пересобирается. При подключении стакан и последние
сделки берутся REST-ом, дальше идут изменения из сокета. Раз в минуту
стакан сверяется с REST: пропущенное удаление уровня не копится.

Без ключа Finam или при ошибке процесс не падает: пишет состояние
источника, а страница показывает его вместо данных.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import websockets
from django.conf import settings
from django.db import close_old_connections

from market.finam import FinamClient, FinamError, parse_decimal
from market.live import JWT_REFRESH, WS_URL, live_symbols, message_symbol, parse_any_timestamp
from market.roll import quarter_points
from radar import algopack
from radar.feed import FLUSH_SECONDS, Radar

DEFAULT_LARGE_PCT = 99.5
DEFAULT_WALL_RATIO = 5.0
RESYNC_SECONDS = 60
# Одна и та же ошибка REST по символу печатается не чаще этого.
ERROR_LOG_EVERY = timedelta(minutes=10)
_logged = {}


# --- разбор сообщений ---

def field(node, snake):
    """Сокет Finam шлёт поля в camelCase (sellSize), спецификация и REST — в snake_case."""
    if snake in node:
        return node[snake]
    head, *rest = snake.split("_")
    return node.get(head + "".join(part.title() for part in rest))


def payload_of(message):
    payload = message.get("payload") if isinstance(message, dict) else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def book_rows(row_list):
    """Строки стакана Finam → (bid/ask, цена, объём, действие)."""
    rows = []
    for row in row_list or []:
        if not isinstance(row, dict):
            continue
        price = float(parse_decimal(row.get("price")))
        action = row.get("action") or "ACTION_UNSPECIFIED"
        sell, buy = field(row, "sell_size"), field(row, "buy_size")
        if sell not in (None, ""):
            rows.append(("ask", price, float(parse_decimal(sell)), action))
        elif buy not in (None, ""):
            rows.append(("bid", price, float(parse_decimal(buy)), action))
    return rows


def books_of(payload):
    """[(символ, строки)] из сокета ({"orderBook": [...]}) и из REST ({"orderbook": {...}})."""
    found = []
    items = field(payload, "order_book")
    if isinstance(items, dict):
        items = [items]
    for item in items or []:
        if isinstance(item, dict):
            found.append((item.get("symbol") or payload.get("symbol") or "", book_rows(item.get("rows"))))
    rest = payload.get("orderbook")
    if isinstance(rest, dict):
        found.append((payload.get("symbol") or "", book_rows(rest.get("rows"))))
    return found


SIDES = {"SIDE_BUY": 1, "SIDE_SELL": -1, "BUY": 1, "SELL": -1}


def trades_of(payload):
    trades = []
    for item in payload.get("trades") or []:
        if not isinstance(item, dict):
            continue
        ts = parse_any_timestamp(item.get("timestamp"))
        if ts is None:
            continue
        oi = field(item, "open_interest")
        trades.append({
            "trade_id": str(field(item, "trade_id") or ""),
            "ts": ts,
            "price": float(parse_decimal(item.get("price"))),
            "size": float(parse_decimal(item.get("size"))),
            "side": SIDES.get(item.get("side"), 0),
            "oi": None if oi in (None, "") else float(parse_decimal(oi)),
            "snapshot": bool(field(item, "is_data_snapshot")),
        })
    return payload.get("symbol") or "", trades


# --- список символов ---

def watch_items(now=None):
    """Ноги спредов и инструменты радара. Инструмент из админки переписывает пороги ноги."""
    from market.models import Instrument
    from radar.models import WatchItem

    close_old_connections()
    now = now or datetime.now(timezone.utc)
    items = {}
    legs = live_symbols()
    titles = dict(
        Instrument.objects.filter(finam_symbol__in=legs).values_list("finam_symbol", "code")
    )
    for symbol in legs:
        items[symbol] = {
            "symbol": symbol, "title": titles.get(symbol, symbol.split("@")[0]), "group": "legs",
            "large_pct": DEFAULT_LARGE_PCT, "wall_ratio": DEFAULT_WALL_RATIO,
        }
    for item in WatchItem.objects.filter(active=True):
        symbol, title = item.code, item.title
        if item.kind == WatchItem.Kind.QUARTER:
            rows = [
                row for row in quarter_points(item.code, now - timedelta(days=1), now + timedelta(days=400))
                if row["roll_at"] > now
            ]
            if not rows:
                continue
            symbol = f"{rows[0]['secid']}@RTSX"
            title = f"{item.title} {rows[0]['code']}" if item.title else rows[0]["code"]
        group = items[symbol]["group"] if symbol in items else "extra"
        items[symbol] = {
            "symbol": symbol, "title": title or symbol.split("@")[0], "group": group,
            "large_pct": item.large_pct, "wall_ratio": item.wall_ratio,
        }
    return list(items.values())


# --- процесс ---

def missing(exc):
    """Finam не знает такого символа: подписываться на него бессмысленно."""
    return isinstance(exc, FinamError) and (exc.status == 404 or "doesn't exist" in str(exc))


def _quiet_log(log, symbol, exc):
    key = (symbol, getattr(exc, "status", None))
    now = datetime.now(timezone.utc)
    if key in _logged and now - _logged[key] < ERROR_LOG_EVERY:
        return
    _logged[key] = now
    log(f"{symbol}: REST {exc}".strip())


def _snapshot(radar, symbols, log, trades=True):
    """Стакан и последние сделки REST-ом. Ошибка по одному символу остальным не мешает.

    Вернуть символы, которых Finam не знает.
    """
    now = datetime.now(timezone.utc)
    unknown = set()
    with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
        for symbol in symbols:
            for fetch in ((client.orderbook, client.latest_trades) if trades else (client.orderbook,)):
                try:
                    payload = fetch(symbol)
                except Exception as exc:
                    if missing(exc):
                        unknown.add(symbol)
                        break
                    _quiet_log(log, symbol, exc)
                    continue
                if fetch == client.orderbook:
                    for _symbol, rows in books_of(payload):
                        radar.on_book(symbol, rows, now, replace=True)
                else:
                    _symbol, items = trades_of(payload)
                    radar.on_trades(symbol, items, detect=False)
    return unknown


async def _resync(radar, symbols, log):
    while True:
        await asyncio.sleep(RESYNC_SECONDS)
        try:
            await asyncio.to_thread(_snapshot, radar, symbols, log, False)
        except Exception as exc:
            log(f"сверка стакана не удалась: {exc}")


async def _socket(radar, symbols, token, log, dump, unknown=()):
    headers = {"Authorization": f"Bearer {token}"}
    errors = 0
    async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=20, ping_timeout=60) as socket:
        for symbol in symbols:
            for kind in ("ORDER_BOOK", "INSTRUMENT_TRADES"):
                await socket.send(json.dumps({"action": "SUBSCRIBE", "type": kind, "data": {"symbol": symbol}, "token": token}))
        log(f"подписка стакана и ленты: {', '.join(symbols)}")
        skipped = f"; Finam не знает: {', '.join(sorted(unknown))}" if unknown else ""
        radar.source("finam", "ok", f"поток идёт, символов {len(symbols)}{skipped}")
        async for raw in socket:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            if dump[0] > 0:
                dump[0] -= 1
                log("сокет: " + text[:800])
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            kind = message.get("type")
            if kind == "ERROR":
                errors += 1
                log(f"Finam ошибка: {message.get('error_info') or message}")
                radar.source("finam", "ok", f"поток идёт, символов {len(symbols)}, ошибок подписки {errors}{skipped}")
                continue
            if kind == "EVENT":
                log(f"Finam событие: {message.get('event_info')}")
                continue
            payload = payload_of(message)
            fallback = message_symbol(message)
            now = datetime.now(timezone.utc)
            for symbol, rows in books_of(payload):
                radar.on_book(symbol or fallback, rows, now)
            symbol, trades = trades_of(payload)
            if trades:
                radar.on_trades(symbol or fallback, trades)


async def _flush_loop(radar, log):
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        try:
            trades, found = await asyncio.to_thread(radar.flush)
            if found:
                log(f"аномалий {found}, сделок {trades}")
        except Exception as exc:
            log(f"запись не удалась: {exc}")


async def _algopack_loop(radar, log):
    while True:
        state, message = await asyncio.to_thread(algopack.check)
        radar.source("algopack", state, message)
        log(f"AlgoPack: {message}")
        await asyncio.sleep(algopack.CHECK_EVERY)


async def run_forever(log, dump=0):
    radar = Radar()
    radar.source("finam", "wait", "запуск")
    tasks = [asyncio.create_task(_flush_loop(radar, log)), asyncio.create_task(_algopack_loop(radar, log))]
    counter = [dump]
    try:
        while True:
            items = await asyncio.to_thread(watch_items)
            await asyncio.to_thread(radar.watch, items)
            symbols = [item["symbol"] for item in items]
            if not settings.FINAM_API_SECRET:
                radar.source("finam", "no_key", "нет FINAM_API_SECRET в .env")
                log("нет FINAM_API_SECRET: страница работает без данных Finam")
                await asyncio.sleep(JWT_REFRESH)
                continue
            log("символы: " + ", ".join(symbols))
            try:
                with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
                    token = await asyncio.to_thread(client.token)
            except Exception as exc:
                radar.source("finam", "error", f"JWT не получен: {exc}")
                log(f"JWT не получен: {exc}. повтор через 15 с")
                await asyncio.sleep(15)
                continue
            radar.source("finam", "wait", f"снимок стаканов, символов {len(symbols)}")
            unknown = await asyncio.to_thread(_snapshot, radar, symbols, log)
            if unknown:
                log("Finam не знает символов, пропускаю до следующего переоткрытия: " + ", ".join(sorted(unknown)))
                items = [item for item in items if item["symbol"] not in unknown]
                symbols = [item["symbol"] for item in items]
                await asyncio.to_thread(radar.watch, items)
            resync = asyncio.create_task(_resync(radar, symbols, log))
            try:
                await asyncio.wait_for(_socket(radar, symbols, token, log, counter, unknown), timeout=JWT_REFRESH)
                log("сокет закрылся, переподключение")
            except asyncio.TimeoutError:
                log("обновляю JWT и список символов")
            except Exception as exc:
                radar.source("finam", "error", f"сокет: {exc}")
                log(f"сокет упал: {exc}. повтор через 5 с")
                await asyncio.sleep(5)
            finally:
                resync.cancel()
    finally:
        for task in tasks:
            task.cancel()
        try:
            await asyncio.to_thread(radar.flush)
        except Exception as exc:
            log(f"последняя запись не удалась: {exc}")
