"""Состояние run_radar в памяти и запись в базу пачкой раз в секунду.

Сокет кладёт сюда сделки и строки стакана, flush раз в FLUSH_SECONDS пишет:
новые сделки, минуты (закрытые и текущую), аномалии, верх стакана и
сводку каждого символа одной строкой BookState, состояние источников.
Сокет живёт в потоке asyncio, запись — в отдельном потоке, отсюда замок.

Правила сигналов перечитываются из базы раз в RULES_EVERY: правка в админке
доходит до детекторов без перезапуска run_radar.
"""

import statistics
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from django.db import close_old_connections

from radar.detect import KEEP_MINUTES, SymbolWatch
from radar.rules import PARAMS, resolve

FLUSH_SECONDS = 1
TRADE_DAYS = 3
ANOMALY_DAYS = 90
MINUTE_DAYS = 365
SEASON_DAYS = 20
CLEANUP_EVERY = timedelta(hours=1)
RULES_EVERY = timedelta(seconds=30)
SEEN_TRADES = 20000


class Radar:
    def __init__(self):
        self.lock = threading.Lock()
        self.watches = {}
        self.meta = {}
        self.trades = []
        self.anomalies = []
        self.seen = {}
        self.cooldown = {}
        self.hourly = {}
        self.sources = {}
        self.dropped = set()
        self.last_data = None
        self.cleaned = None
        self.rule_rows = []
        self.rules_at = None

    # --- список символов ---

    def watch(self, items):
        """items: словари symbol, title, group. Состояние старых символов сохраняется."""
        if self.rules_at is None:
            self.reload_rules()
        wanted = {item["symbol"]: item for item in items}
        fresh = [symbol for symbol in wanted if symbol not in self.watches]
        prepared = {symbol: _history(symbol) for symbol in fresh}
        with self.lock:
            for symbol in list(self.watches):
                if symbol not in wanted:
                    del self.watches[symbol]
                    self.meta.pop(symbol, None)
                    self.dropped.add(symbol)
            for symbol, item in wanted.items():
                watch = self.watches.get(symbol)
                if watch is None:
                    watch = SymbolWatch(symbol, resolve(symbol, self.rule_rows))
                    sizes, seasonal, recent, day_open = prepared[symbol]
                    watch.sizes.sizes.extend(sizes)
                    watch.seasonal = seasonal
                    watch.recent.extend(recent)
                    if day_open is not None:
                        watch.day, watch.day_open = day_open
                    self.watches[symbol] = watch
                self.meta[symbol] = {"title": item["title"], "group": item["group"]}
                self.dropped.discard(symbol)

    def reload_rules(self, now=None):
        """Правила сигналов из базы — в детекторы всех символов."""
        from radar.models import SignalRule

        close_old_connections()
        rows = list(SignalRule.objects.values("kind", "scope", "enabled", *PARAMS))
        with self.lock:
            self.rule_rows = rows
            self.rules_at = now or datetime.now(timezone.utc)
            for symbol, watch in self.watches.items():
                watch.rules = resolve(symbol, rows)

    def symbols(self):
        with self.lock:
            return list(self.watches)

    # --- потоки ---

    def on_trades(self, symbol, trades, detect=True):
        with self.lock:
            watch = self.watches.get(symbol)
            if watch is None:
                return
            seen = self.seen.setdefault(symbol, (set(), deque()))
            for trade in sorted(trades, key=lambda item: item["ts"]):
                key = trade["trade_id"] or f"{trade['ts'].isoformat()}|{trade['price']}|{trade['size']}"
                if key in seen[0]:
                    continue
                seen[0].add(key)
                seen[1].append(key)
                if len(seen[1]) > SEEN_TRADES:
                    seen[0].discard(seen[1].popleft())
                self.trades.append({**trade, "trade_id": key[:40], "symbol": symbol})
                self._found(watch.on_trade(trade, detect=detect and not trade.get("snapshot")))
            self.last_data = datetime.now(timezone.utc)

    def on_book(self, symbol, rows, ts, replace=False):
        with self.lock:
            watch = self.watches.get(symbol)
            if watch is None:
                return
            self._found(watch.on_book(rows, ts, replace=replace, detect=not replace))
            self.last_data = datetime.now(timezone.utc)

    def tick(self, now):
        with self.lock:
            for watch in self.watches.values():
                self._found(watch.tick(now))

    def source(self, name, state, message=""):
        with self.lock:
            self.sources[name] = (state, message[:255])

    def _found(self, items):
        """Выключенный сигнал не пишется, повтор раньше паузы правила — тоже.
        У объёма и ОИ пауза не короче окна: иначе один всплеск даёт событие каждую минуту окна."""
        for item in items:
            rule = self.watches[item["symbol"]].rules[item["kind"]]
            if not rule["enabled"]:
                continue
            key = (item["symbol"], item["kind"], item["side"])
            limit = max(rule["cooldown_seconds"], rule.get("window_minutes", 0) * 60)
            last = self.cooldown.get(key)
            if limit and last is not None and item["ts"] - last < timedelta(seconds=limit):
                continue
            self.cooldown[key] = item["ts"]
            self.anomalies.append(item)
            self.hourly.setdefault(item["symbol"], deque()).append(item["ts"])

    # --- запись ---

    def flush(self, now=None):
        """Всё накопленное — в базу. Вернуть (сделок, аномалий)."""
        from radar.models import Anomaly, BookState, MinuteStat, SourceStatus, Trade

        now = now or datetime.now(timezone.utc)
        if self.rules_at is None or now - self.rules_at >= RULES_EVERY:
            self.reload_rules(now)
        self.tick(now)
        with self.lock:
            trades, self.trades = self.trades, []
            anomalies, self.anomalies = self.anomalies, []
            minutes = []
            books = []
            for symbol, watch in self.watches.items():
                minutes.extend({**bar, "symbol": symbol} for bar in watch.closed)
                watch.closed = []
                if watch.bar is not None:
                    minutes.append({key: value for key, value in watch.bar.items() if key != "quiet"} | {"symbol": symbol})
                asks, bids = watch.levels()
                books.append(BookState(
                    symbol=symbol, title=self.meta[symbol]["title"], group=self.meta[symbol]["group"],
                    ts=self.last_data, asks=asks, bids=bids, summary=self._summary(symbol, watch, now),
                    updated_at=now,
                ))
            dropped, self.dropped = set(self.dropped), set()
            sources = dict(self.sources)
            count = len(self.watches)
        close_old_connections()
        if trades:
            Trade.objects.bulk_create([
                Trade(
                    symbol=item["symbol"], trade_id=item["trade_id"], ts=item["ts"], price=item["price"],
                    size=item["size"], side=item["side"], oi=item.get("oi"),
                )
                for item in trades
            ], ignore_conflicts=True, batch_size=1000)
        if minutes:
            by_key = {(item["symbol"], item["ts"]): item for item in minutes}
            MinuteStat.objects.bulk_create(
                [MinuteStat(**item) for item in by_key.values()],
                update_conflicts=True, unique_fields=["symbol", "ts"],
                update_fields=["open", "high", "low", "close", "volume", "buy", "sell", "trades", "large", "oi"],
            )
        if anomalies:
            Anomaly.objects.bulk_create([Anomaly(**item) for item in anomalies])
        if books:
            BookState.objects.bulk_create(
                books, update_conflicts=True, unique_fields=["symbol"],
                update_fields=["title", "group", "ts", "asks", "bids", "summary", "updated_at"],
            )
        if dropped:
            BookState.objects.filter(symbol__in=dropped).delete()
        sources["radar"] = ("ok", f"символов {count}")
        SourceStatus.objects.bulk_create(
            [SourceStatus(name=name, state=state, message=message, updated_at=now) for name, (state, message) in sources.items()],
            update_conflicts=True, unique_fields=["name"], update_fields=["state", "message", "updated_at"],
        )
        if self.cleaned is None or now - self.cleaned > CLEANUP_EVERY:
            self.cleaned = now
            cleanup(now)
        return len(trades), len(anomalies)

    def _summary(self, symbol, watch, now):
        hour = self.hourly.get(symbol, deque())
        while hour and now - hour[0] > timedelta(hours=1):
            hour.popleft()
        last = watch.last_price
        recent = list(watch.recent)[-15:]
        norm = watch.season_sample(now) or list(watch.recent)
        ratio = None
        if recent and len(norm) >= 10:
            expected = statistics.median(norm) * len(recent)
            ratio = None if expected <= 0 else round(sum(recent) / expected, 2)
        change = None
        if last is not None and watch.day_open:
            change = round((last / watch.day_open - 1) * 100, 3)
        return {
            "last": last,
            "change": change,
            "volume_ratio": ratio,
            "anomalies": len(hour),
            "oi": watch.last_oi if watch.bar is None or watch.bar["oi"] is None else watch.bar["oi"],
        }


def _history(symbol):
    """Размеры последних сделок, объёмы тех же минут прошлых дней, сегодняшние минуты, открытие дня."""
    from radar.models import MinuteStat, Trade

    close_old_connections()
    now = datetime.now(timezone.utc)
    sizes = [
        float(size) for size in
        Trade.objects.filter(symbol=symbol).order_by("-ts").values_list("size", flat=True)[:5000]
    ][::-1]
    day_start = datetime.combine((now + timedelta(hours=3)).date(), datetime.min.time(), timezone.utc) - timedelta(hours=3)
    seasonal = {}
    recent = []
    day_open = None
    rows = (
        MinuteStat.objects.filter(symbol=symbol, ts__gte=now - timedelta(days=SEASON_DAYS))
        .order_by("ts").values_list("ts", "volume", "open")
    )
    for ts, volume, opened in rows:
        if ts >= day_start:
            recent.append(float(volume))
            if day_open is None:
                day_open = ((ts + timedelta(hours=3)).date(), float(opened))
        else:
            seasonal.setdefault(ts.hour * 60 + ts.minute, []).append(float(volume))
    return sizes, seasonal, recent[-KEEP_MINUTES:], day_open


def cleanup(now):
    from radar.models import Anomaly, MinuteStat, Trade

    Trade.objects.filter(ts__lt=now - timedelta(days=TRADE_DAYS)).delete()
    Anomaly.objects.filter(ts__lt=now - timedelta(days=ANOMALY_DAYS)).delete()
    MinuteStat.objects.filter(ts__lt=now - timedelta(days=MINUTE_DAYS)).delete()
