"""SHIBOR O/N. ЦБ эту ставку не публикует.

История: China Money, поле ON, дата showDateCN.
Длинный интервал сервис отдаёт пустым, поэтому запрос идёт по календарным годам.
Свой CSV с колонками date,O/N — тот же формат, что SHIBOR.csv у скрипта.
"""

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from rates.cbr import year_spans

HISTORY_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
SOURCE = "SHIBOR"


def fetch_shibor(start: date, end: date) -> list[tuple[date, Decimal]]:
    rows = {}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    with httpx.Client(timeout=60.0, headers=headers) as client:
        for chunk_start, chunk_end in year_spans(start, end):
            response = client.get(HISTORY_URL, params={
                "lang": "cn",
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
            })
            response.raise_for_status()
            for record in response.json().get("records") or []:
                day = date.fromisoformat(record["showDateCN"][:10])
                if start <= day <= end and record.get("ON") not in (None, ""):
                    rows[day] = Decimal(str(record["ON"]))
    return sorted(rows.items())


def read_shibor_csv(path) -> list[tuple[date, Decimal]]:
    path = Path(path)
    rows = []
    with path.open(newline="") as handle:
        for record in csv.DictReader(handle):
            raw_day = record.get("date") or record.get("Date")
            raw_rate = record.get("O/N") or record.get("ON") or record.get("o/n")
            if not raw_day or raw_rate in (None, ""):
                continue
            rows.append((date.fromisoformat(raw_day[:10]), Decimal(str(raw_rate))))
    return rows
