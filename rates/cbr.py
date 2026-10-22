"""RUONIA с SOAP ЦБ. Одна ставка на календарный день, проценты годовых.

Дата берётся как календарный день из ответа (МСК), без сдвига в UTC.
"""

from datetime import date, timedelta
from decimal import Decimal
import xml.etree.ElementTree as ET

import httpx

ENDPOINT = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
SOAP_ACTION = "http://web.cbr.ru/Ruonia"


def year_spans(start: date, end: date):
    if end < start:
        raise ValueError("конец раньше начала")
    cursor = start
    while cursor <= end:
        chunk_end = min(date(cursor.year, 12, 31), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def fetch_ruonia(start: date, end: date) -> list[tuple[date, Decimal]]:
    rows = {}
    with httpx.Client(timeout=60.0) as client:
        for chunk_start, chunk_end in year_spans(start, end):
            response = client.post(ENDPOINT, content=_body(chunk_start, chunk_end), headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": SOAP_ACTION,
            })
            response.raise_for_status()
            for day, rate in parse_ruonia(response.text):
                if start <= day <= end:
                    rows[day] = rate
    return sorted(rows.items())


def parse_ruonia(xml_text: str) -> list[tuple[date, Decimal]]:
    root = ET.fromstring(xml_text)
    rows = []
    for node in root.iter():
        if _local(node.tag) != "ro":
            continue
        raw_day = raw_rate = None
        for child in list(node):
            name = _local(child.tag)
            if name == "D0":
                raw_day = child.text
            elif name == "ruo":
                raw_rate = child.text
        if raw_day and raw_rate:
            rows.append((date.fromisoformat(raw_day[:10]), Decimal(raw_rate)))
    return rows


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _body(start: date, end: date) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<Ruonia xmlns="http://web.cbr.ru/">'
        f"<fromDate>{start.isoformat()}</fromDate>"
        f"<ToDate>{end.isoformat()}</ToDate>"
        "</Ruonia></soap:Body></soap:Envelope>"
    )
