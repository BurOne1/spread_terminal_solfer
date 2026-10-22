"""DFEDTARU с FRED. Ступенчатый ряд: значение держится до следующей даты.

Сначала короткий CSV с 2023 года, если он молчит — полный текстовый ряд.
Обе ссылки с коротким таймаутом: зависший FRED не должен держать остальные ставки.
Если сайт не отвечает, тот же CSV можно скачать вручную и отдать команде.
"""

import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

URLS = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU&cosd=2023-01-01",
    "https://fred.stlouisfed.org/data/DFEDTARU.txt",
)
SOURCE = "DFEDTARU"
TIMEOUT = httpx.Timeout(25.0, connect=10.0)


def fetch_dfedtaru(start: date, end: date) -> list[tuple[date, Decimal]]:
    last = None
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,text/plain"}
    for url in URLS:
        for attempt in range(2):
            try:
                with httpx.Client(timeout=TIMEOUT, follow_redirects=True, http2=False) as client:
                    response = client.get(url, headers=headers)
                    response.raise_for_status()
                rows = parse_dfedtaru(response.text, start, end)
                if rows:
                    return rows
                last = RuntimeError(f"пустой ответ {url}")
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                time.sleep(2)
    raise last


def read_dfedtaru_csv(path, start: date, end: date) -> list[tuple[date, Decimal]]:
    return parse_dfedtaru(Path(path).read_text(encoding="utf-8-sig"), start, end)


def parse_dfedtaru(text: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("date", "observation", "title", "source", "release")):
            continue
        if "," in line:
            raw_day, raw_rate = line.split(",", 1)
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            raw_day, raw_rate = parts[0], parts[1]
        raw_rate = raw_rate.strip().strip('"')
        if len(raw_day) < 10 or raw_rate in ("", "."):
            continue
        try:
            day = date.fromisoformat(raw_day[:10])
            rate = Decimal(raw_rate)
        except Exception:
            continue
        if start <= day <= end:
            rows.append((day, rate))
    return rows