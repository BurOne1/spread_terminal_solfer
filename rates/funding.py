"""Фандинг вечных с ISS Мосбиржи.

SWAPRATE — рубли на единицу котировки. В базу пишется SWAPRATE × лот:
рубли на контракт за день. У каждой пары свой столбец.
Валюта — лот 1000, золото — 1 грамм, серебро — 100 граммов при цене за грамм.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from rates.models import FundingDay

SERIES = {
    "swaprate_rub": ("CNYRUBF", Decimal("1000")),
    "swaprate_usd": ("USDRUBF", Decimal("1000")),
    "swaprate_eur": ("EURRUBF", Decimal("1000")),
    "swaprate_gold": ("GLDRUBF", Decimal("1")),
    "swaprate_silver": ("SLVRUBF", Decimal("100")),
}
FIELDS = {
    "USD": "swaprate_usd",
    "EUR": "swaprate_eur",
    "GOLD": "swaprate_gold",
    "SILVER": "swaprate_silver",
}
LOT = Decimal("1000")
PAGE = 100


def funding_field(pair):
    if pair is None:
        return "swaprate_rub"
    return FIELDS.get(pair.code, "swaprate_rub")


def fetch_swap(secid: str, start: date, end: date, lot: Decimal = LOT) -> list[tuple[date, Decimal]]:
    url = f"https://iss.moex.com/iss/history/engines/futures/markets/forts/securities/{secid}.json"
    rows = {}
    start_at = 0
    with httpx.Client(timeout=60.0) as client:
        while True:
            response = client.get(url, params={
                "iss.meta": "off",
                "from": start.isoformat(),
                "till": end.isoformat(),
                "start": start_at,
                "history.columns": "TRADEDATE,SWAPRATE",
            })
            response.raise_for_status()
            payload = response.json()
            history = payload.get("history") or {}
            columns = history.get("columns") or []
            data = history.get("data") or []
            if not data or "TRADEDATE" not in columns or "SWAPRATE" not in columns:
                break
            day_i = columns.index("TRADEDATE")
            rate_i = columns.index("SWAPRATE")
            for row in data:
                raw = row[rate_i]
                if raw in (None, ""):
                    continue
                day = date.fromisoformat(str(row[day_i])[:10])
                if start <= day <= end:
                    rows[day] = Decimal(str(raw)) * lot
            cursor = (payload.get("history.cursor") or {}).get("data") or [[0, 0, PAGE]]
            total = cursor[0][1]
            start_at += len(data)
            if start_at >= total:
                break
    return sorted(rows.items())


def fill_funding(lookback=14):
    end = datetime.now(timezone.utc).date()
    result = {"from": end, "to": end, "days": 0, "last": None}
    for field, spec in SERIES.items():
        secid, lot = spec
        last = (
            FundingDay.objects.filter(**{f"{field}__isnull": False})
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        start = (last + timedelta(days=1)) if last else date(2023, 1, 1)
        if start > end:
            start = end - timedelta(days=lookback)
        rows = fetch_swap(secid, start, end, lot)
        for day, value in rows:
            FundingDay.objects.update_or_create(date=day, defaults={field: value, "source": "moex"})
        result["days"] += len(rows)
        result["from"] = min(result["from"], start)
        if rows:
            result["last"] = rows[-1]
    return result