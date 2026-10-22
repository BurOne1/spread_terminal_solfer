"""Публичный Bybit V5. Ключ не нужен.

Линейные свечи и фандинг: category=linear, символ BTCUSDT или BTCUSDT-27MAR26.
Спот: category=spot, символ BTCUSDT.
"""

from datetime import datetime, timezone
from decimal import Decimal

import httpx

BASE = "https://api.bybit.com"


class BybitError(RuntimeError):
    pass


def _ms(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _get(path: str, params: dict) -> dict:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(BASE + path, params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") not in (0, "0"):
        raise BybitError(payload.get("retMsg") or payload)
    return payload.get("result") or {}


def instruments(category: str, base_coin: str = "BTC") -> list[dict]:
    rows = []
    cursor = ""
    while True:
        params = {"category": category, "baseCoin": base_coin, "limit": 500}
        if cursor:
            params["cursor"] = cursor
        result = _get("/v5/market/instruments-info", params)
        rows.extend(result.get("list") or [])
        cursor = result.get("nextPageCursor") or ""
        if not cursor:
            return rows


def klines(symbol: str, start: datetime, end: datetime, interval: str = "60", category: str = "linear") -> list[dict]:
    rows = {}
    cursor_end = _ms(end)
    start_ms = _ms(start)
    while cursor_end > start_ms:
        result = _get("/v5/market/kline", {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "start": start_ms,
            "end": cursor_end,
            "limit": 1000,
        })
        batch = result.get("list") or []
        if not batch:
            break
        oldest = cursor_end
        for item in batch:
            ts = datetime.fromtimestamp(int(item[0]) / 1000, timezone.utc)
            oldest = min(oldest, int(item[0]))
            rows[ts] = {
                "ts": ts,
                "open": Decimal(item[1]),
                "high": Decimal(item[2]),
                "low": Decimal(item[3]),
                "close": Decimal(item[4]),
                "volume": Decimal(item[5]),
            }
        if len(batch) < 1000:
            break
        cursor_end = oldest - 1
    return [rows[key] for key in sorted(rows)]


def funding_history(symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]:
    rows = {}
    cursor_end = _ms(end)
    start_ms = _ms(start)
    while cursor_end > start_ms:
        result = _get("/v5/market/funding/history", {
            "category": "linear",
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": cursor_end,
            "limit": 200,
        })
        batch = result.get("list") or []
        if not batch:
            break
        oldest = cursor_end
        for item in batch:
            stamp = int(item["fundingRateTimestamp"])
            ts = datetime.fromtimestamp(stamp / 1000, timezone.utc)
            oldest = min(oldest, stamp)
            rows[ts] = Decimal(item["fundingRate"])
        if len(batch) < 200:
            break
        cursor_end = oldest - 1
    return sorted(rows.items())
