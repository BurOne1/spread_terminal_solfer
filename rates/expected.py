"""Фандинг вечного пары для вкладки «Фандинг» на странице графика (/api/funding/).

Ожидаемый фандинг на сегодня — SWAPRATE из текущих данных торгов Мосбиржи (ISS marketdata),
рубли на единицу котировки (1 доллар, 1 грамм, 1 монету). История — FundingDay
(rates/funding.py пишет SWAPRATE × лот, здесь делится обратно на лот). Средние —
за последние 3, 5 и 10 дней истории. % годовых = фандинг / цена вечного × 365 × 100.

Какой вечный у пары — как в strategy.runner.funding_of: свой у USD, EUR, CNY, GOLD,
SILVER, BTCMOEX, ETHMOEX; у треугольников — вечный металла (GLDRUBF, SLVRUBF);
у TRIGL, TRIGDS, TRISVS, календарей и пар со справедливым нулём фандинга нет.
"""

import time
from datetime import timedelta, timezone

import httpx

ISS = "https://iss.moex.com/iss/engines/futures/markets/forts/securities/{}.json"
CACHE_SECONDS = 60
MSK = timezone(timedelta(hours=3))
_cache = {}


def perp_of(pair):
    """(secid вечного, лот, поле FundingDay) или None, если у пары фандинга нет."""
    from market.models import Pair
    from rates.funding import SERIES, funding_field
    if pair.calendar or pair.zero_fair:
        return None
    if pair.foreign_source == "TRI":
        from strategy.rebuild import TRI_PERP, TRI_SPOT
        if pair.tri_code == "TRIGL" or pair.tri_code in TRI_SPOT:
            return None
        field = funding_field(Pair.objects.filter(code=TRI_PERP.get(pair.tri_code, "")).first())
    else:
        field = funding_field(pair)
        if SERIES.get(field, ("",))[0] != pair.perp_code:
            return None  # своя пара с другим вечным — его фандинга у нас нет
    secid, lot = SERIES[field]
    return secid, float(lot), field


def market(secid, fetch=None):
    """{"swaprate", "last", "settle", "time"} из ISS marketdata, кэш на CACHE_SECONDS."""
    now = time.monotonic()
    cached = _cache.get(secid)
    if cached is None or now - cached[0] > CACHE_SECONDS:
        cached = (now, (fetch or _fetch)(secid))
        _cache[secid] = cached
    return cached[1]


def _fetch(secid):
    response = httpx.get(ISS.format(secid), params={
        "iss.only": "marketdata", "iss.meta": "off",
        "marketdata.columns": "SECID,SWAPRATE,LAST,SETTLEPRICE,SYSTIME",
    }, timeout=5.0)
    response.raise_for_status()
    block = response.json().get("marketdata") or {}
    rows = [dict(zip(block.get("columns") or [], row)) for row in block.get("data") or []]
    row = rows[0] if rows else {}
    number = lambda key: float(row[key]) if row.get(key) not in (None, "") else None  # noqa: E731
    return {"swaprate": number("SWAPRATE"), "last": number("LAST"), "settle": number("SETTLEPRICE"), "time": row.get("SYSTIME") or ""}


def panel(pair, fetch=None):
    """Данные вкладки: вечный, ожидаемый фандинг, история и средние, % годовых."""
    from rates.models import FundingDay
    found = perp_of(pair)
    if found is None:
        return {"perp": "", "note": "у пары нет вечного фьючерса — фандинга нет"}
    secid, lot, field = found
    days = [
        (day, float(value) / lot)
        for day, value in FundingDay.objects.exclude(**{f"{field}__isnull": True})
        .order_by("-date").values_list("date", field)[:10]
    ]
    note = ""
    try:
        live = market(secid, fetch)
    except Exception as exc:
        live = {"swaprate": None, "last": None, "settle": None, "time": ""}
        note = f"Мосбиржа не ответила: {type(exc).__name__}"
    price = live.get("last") or live.get("settle")

    def annual(value):
        return None if value is None or not price else value / price * 365 * 100

    def average(count):
        values = [value for _day, value in days[:count]]
        return sum(values) / len(values) if len(values) == count else None

    rows = []
    if days:
        rows.append({"name": "F_lastday", "value": days[0][1], "annual": annual(days[0][1]), "hint": f"за {days[0][0]:%d.%m}"})
    for count in (3, 5, 10):
        value = average(count)
        if value is not None:
            rows.append({"name": f"F_avg{count}d", "value": value, "annual": annual(value), "hint": f"среднее за {count} дн."})
    expected = live.get("swaprate")
    return {
        "perp": secid, "lot": lot, "price": price, "time": live.get("time", ""),
        "expected": expected, "annual": annual(expected),
        "per_contract": None if expected is None else expected * lot,
        "rows": rows, "note": note,
    }
