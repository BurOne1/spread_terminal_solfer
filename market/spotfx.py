"""Кросс-курсы на споте: CR × USDCNY × 1000 − Si и Si × EURUSD − Eu, в пунктах Si и Eu.

Логика как у CRCNH и SI6E (market/cross.py), только третья нога — не фьючерс
CME, а спот USDCNY (юани за доллар) и EURUSD (доллары за евро). Справедливое
значение — ноль.

Окна — кварталы Мосбиржи ноги c (Si у пары USD, Eu у пары EUR), нога a —
квартал того же месяца (CR у CNY, Si у USD). Своих кварталов у пар нет,
свой только спот: инструмент USDCNY или EURUSD без символа Finam.

Спот приходит из сервиса котировок FX_CHECK_URL (/check): JSON-список
последних котировок {"ticker", "bid", "ask", "utcdt"}, по 10 на тикер, истории
нет. run_live опрашивает его раз в FX_EVERY секунд, середина bid/ask идёт в
свечи 1ч и 5м так же, как котировки сокета (market.live.book_quote).
История — CSV TradingView через import_tv: файл, в имени которого есть
USDCNY или EURUSD («FX_IDC_USDCNY, 5.csv»).
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from market.models import Instrument

SOURCE = "SPOTFX"
FX_EVERY = 5

SPOTFX = {
    "CRSISPOT": {
        "title": "CR × USDCNY − Si", "a": "CR", "c": "Si", "spot": "USDCNY", "mult": 1000,
        "a_owner": "CNY", "c_owner": "USD", "note": "рубли за 1000 долларов, пункты Si",
    },
    "SIEUSPOT": {
        "title": "Si × EURUSD − Eu", "a": "Si", "c": "Eu", "spot": "EURUSD", "mult": 1,
        "a_owner": "USD", "c_owner": "EUR", "note": "рубли за 1000 евро, пункты Eu",
    },
}


def deps(code):
    """Пары, чьи свечи меняют спред: сама пара (спот) и владельцы кварталов."""
    spec = SPOTFX[code]
    return (code, spec["a_owner"], spec["c_owner"])


def ensure(pair):
    """Спот пары в базе: USDCNY или EURUSD, без символа Finam. Вернуть инструмент."""
    spec = SPOTFX[pair.code]
    instrument, _created = Instrument.objects.update_or_create(
        code=spec["spot"],
        defaults={"pair": pair, "kind": Instrument.Kind.SPOT, "finam_symbol": "", "lot": 1},
    )
    return instrument


def parse_time(text):
    """«19-11-2026 11:07:46.000000» (UTC) → datetime или None."""
    try:
        return datetime.strptime(text.strip(), "%d-%m-%Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None


def parse_check(payload, tickers):
    """Ответ /check → {тикер: [(время, середина bid/ask), ...] по времени} только для tickers."""
    out = {}
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict) or row.get("msg", "quote") != "quote":
            continue
        ticker = row.get("ticker")
        if ticker not in tickers:
            continue
        ts = parse_time(row.get("utcdt") or "")
        try:
            bid, ask = Decimal(str(row.get("bid"))), Decimal(str(row.get("ask")))
        except (InvalidOperation, TypeError):
            continue
        if ts is None or bid <= 0 or ask <= 0:
            continue
        out.setdefault(ticker, []).append((ts, (bid + ask) / 2))
    for rows in out.values():
        rows.sort(key=lambda item: item[0])
    return out


def fetch(url, timeout=5.0):
    import httpx
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def new_quotes(quotes, seen):
    """Только котировки новее уже переданных; seen {тикер: время} обновляется."""
    fresh = {}
    for ticker, rows in quotes.items():
        last = seen.get(ticker)
        rows = [row for row in rows if last is None or row[0] > last]
        if rows:
            fresh[ticker] = rows
            seen[ticker] = rows[-1][0]
    return fresh
