"""€STR с ЕЦБ. Однодневная ставка евро, проценты годовых.

CSV: https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?format=csvdata
Если сайт не отвечает, тот же файл можно скачать и отдать команде.
"""

import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

import httpx

URL = "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?format=csvdata"
SOURCE = "ESTR"


def fetch_estr(start: date, end: date) -> list[tuple[date, Decimal]]:
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        response = client.get(URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"})
        response.raise_for_status()
    return parse_estr(response.text, start, end)


def read_estr_csv(path, start: date, end: date) -> list[tuple[date, Decimal]]:
    return parse_estr(Path(path).read_text(encoding="utf-8-sig"), start, end)


def parse_estr(text: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    rows = []
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        return rows
    day_key = _column(reader.fieldnames, "TIME_PERIOD", "date", "DATE")
    value_key = _column(reader.fieldnames, "OBS_VALUE", "value", "ESTR", "estr")
    if not day_key or not value_key:
        raise ValueError(f"в CSV €STR нет даты или значения: {reader.fieldnames}")
    for record in reader:
        raw_day = (record.get(day_key) or "")[:10]
        raw_value = (record.get(value_key) or "").strip()
        if len(raw_day) < 10 or raw_value in ("", ".", "NaN"):
            continue
        day = date.fromisoformat(raw_day)
        if start <= day <= end:
            rows.append((day, Decimal(raw_value)))
    return rows


def _column(names, *candidates):
    folded = {name.strip().lower(): name for name in names}
    for candidate in candidates:
        if candidate.lower() in folded:
            return folded[candidate.lower()]
    return ""
