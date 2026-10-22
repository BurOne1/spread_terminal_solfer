"""Finam Trade API: JWT и часовые свечи.

POST /v1/sessions  {"secret": ...} → {"token": ...}. Токен живёт около 15 минут.
GET  /v1/instruments/{symbol}/bars?timeframe=TIME_FRAME_H1

Часовая история у Финама — примерно 30 дней. Команда загрузки ходит окнами
и не удаляет свечи, которые уже лежат в Postgres.
Символ ФОРТС: CNYRUBF@RTSX, квартал CRZ6@RTSX.
"""

import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx

PERP_SYMBOL = "CNYRUBF@RTSX"
FUTURES_MIC = "RTSX"
TOKEN_TTL_SECONDS = 12 * 60
SPOT_SYMBOL = "CNYRUB_TOM@MISX"

class FinamError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def future_symbol(secid: str) -> str:
    return f"{secid}@{FUTURES_MIC}"


def parse_timestamp(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_decimal(node) -> Decimal:
    if isinstance(node, dict):
        node = node.get("value")
    if node in (None, ""):
        return Decimal("0")
    return Decimal(str(node))


def parse_bars(payload: dict) -> list[dict]:
    rows = {}
    for bar in payload.get("bars") or []:
        ts = parse_timestamp(bar["timestamp"])
        rows[ts] = {
            "ts": ts,
            "open": parse_decimal(bar.get("open")),
            "high": parse_decimal(bar.get("high")),
            "low": parse_decimal(bar.get("low")),
            "close": parse_decimal(bar.get("close")),
            "volume": parse_decimal(bar.get("volume")),
        }
    return [rows[key] for key in sorted(rows)]


class FinamClient:
    def __init__(self, secret: str, base_url: str):
        if not secret:
            raise FinamError("Пустой FINAM_API_SECRET")
        self._secret = secret
        self._base = base_url.rstrip("/")
        self._token = None
        self._token_at = 0.0
        self._http = httpx.Client(timeout=40.0)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def token(self) -> str:
        if self._token and (time.monotonic() - self._token_at) < TOKEN_TTL_SECONDS:
            return self._token
        response = self._http.post(f"{self._base}/v1/sessions", json={"secret": self._secret})
        self._raise(response)
        body = response.json()
        if not body.get("token"):
            raise FinamError("Finam не вернул token")
        self._token = body["token"]
        self._token_at = time.monotonic()
        return self._token

    def bars(self, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[dict]:
        params = {
            "timeframe": timeframe,
            "interval.start_time": _iso(start),
            "interval.end_time": _iso(end),
        }
        path = f"{self._base}/v1/instruments/{quote(symbol, safe='')}/bars"
        response = self._get(path, params)
        if response.status_code == 401:
            self._token = None
            response = self._get(path, params)
        self._raise(response, symbol=symbol)
        return parse_bars(response.json())

    def _get(self, path, params):
        return self._http.get(path, params=params, headers={"Authorization": f"Bearer {self.token()}"})

    def _raise(self, response, symbol=""):
        if response.status_code < 400:
            return
        detail = response.text[:300].replace("\n", " ")
        where = f" {symbol}" if symbol else ""
        raise FinamError(f"Finam {response.status_code}{where}: {detail}", status=response.status_code)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
