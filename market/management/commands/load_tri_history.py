"""Загружает историю треугольника одним архивом или папкой.

Внутри: CSV свечей и ruonia.csv. Команда читает и то и другое разом.
CSV: time,open,high,low,close — time это unix-секунды часа открытия.
Имена: RUS_SIM2026, RUS_GDU2026, RUS_GLDRUB.P и так далее.
Инструмент должен уже быть в базе (ensure_contracts / add_tri).

Если ruonia.csv нет, ставка качается с ЦБ за --from/--to.
Пустые дни ставки не затирают уже записанное значение.
"""

import csv
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from market.models import Candle, Instrument
from rates.models import RateDay

FILE_CODE = {
    "GLDRUB.P": "GLDRUBF",
    "SLVRUB.P": "SLVRUBF",
}
SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <RuoniaXML xmlns="http://web.cbr.ru/">
      <fromDate>{start}</fromDate>
      <ToDate>{end}</ToDate>
    </RuoniaXML>
  </soap:Body>
</soap:Envelope>"""


class Command(BaseCommand):
    help = "Загружает CSV истории треугольника и RUONIA одним архивом или папкой."

    def add_arguments(self, parser):
        parser.add_argument("source", help="zip с CSV или папка")
        parser.add_argument("--from", dest="start", default="2025-06-01")
        parser.add_argument("--to", dest="end", default="")

    def handle(self, *args, **options):
        source = Path(options["source"])
        if not source.exists():
            raise CommandError(f"нет {source}")
        start = date.fromisoformat(options["start"])
        end = date.fromisoformat(options["end"]) if options["end"] else date.today()
        with _opened(source) as folder:
            candles = self._candles(folder)
            rates = self._rates(folder, start, end)
        self.stdout.write(f"свечей {candles}, ставок {rates}")

    def _candles(self, folder):
        total = 0
        for path in sorted(folder.glob("*.csv")):
            if path.name.lower() == "ruonia.csv":
                continue
            code = _code_of(path.name)
            instrument = (
                Instrument.objects.filter(code=code).first()
                or Instrument.objects.filter(code__iexact=code).first()
            )
            if instrument is None:
                self.stdout.write(f"  нет инструмента {code}, пропуск {path.name}")
                continue
            rows = list(_read_ohlc(path))
            if not rows:
                continue
            Candle.objects.bulk_create(
                [Candle(instrument=instrument, **row) for row in rows],
                update_conflicts=True,
                unique_fields=["instrument", "ts"],
                update_fields=["open", "high", "low", "close"],
            )
            total += len(rows)
            self.stdout.write(f"  {code}: {len(rows)}  {rows[0]['ts']:%Y-%m-%d} → {rows[-1]['ts']:%Y-%m-%d}")
        return total

    def _rates(self, folder, start, end):
        path = folder / "ruonia.csv"
        rows = _read_ruonia(path) if path.exists() else _fetch_ruonia(start, end)
        if not rows:
            self.stdout.write("  RUONIA пусто")
            return 0
        with transaction.atomic():
            for day, rate in rows:
                RateDay.objects.update_or_create(date=day, defaults={"ruonia": rate})
        self.stdout.write(f"  RUONIA: {len(rows)}  {rows[0][0]} → {rows[-1][0]} = {rows[-1][1]}")
        return len(rows)


class _opened:
    def __init__(self, source):
        self.source = source
        self.temp = None

    def __enter__(self):
        if self.source.is_dir():
            return self.source
        if zipfile.is_zipfile(self.source):
            self.temp = tempfile.TemporaryDirectory()
            with zipfile.ZipFile(self.source) as archive:
                archive.extractall(self.temp.name)
            return Path(self.temp.name)
        raise CommandError(f"{self.source} не папка и не zip")

    def __exit__(self, *exc):
        if self.temp:
            self.temp.cleanup()


def _code_of(name):
    stem = Path(name).stem
    stem = re.sub(r"_[0-9a-f]{4,}$", "", stem, flags=re.I)
    stem = stem.replace("RUS_", "")
    return FILE_CODE.get(stem, stem)


def _read_ohlc(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = row.get("time") or row.get("ts")
            close = row.get("close")
            if not raw or close in (None, ""):
                continue
            ts = datetime.fromtimestamp(int(float(raw)), timezone.utc).replace(
                minute=0, second=0, microsecond=0
            )
            yield {
                "ts": ts,
                "open": Decimal(row.get("open") or close),
                "high": Decimal(row.get("high") or close),
                "low": Decimal(row.get("low") or close),
                "close": Decimal(close),
                "volume": Decimal(row.get("volume") or 0),
            }


def _read_ruonia(path):
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            day = date.fromisoformat(row["date"][:10])
            rows.append((day, Decimal(row["ruonia"])))
    return rows


def _fetch_ruonia(start, end):
    body = SOAP.format(start=start.isoformat(), end=end.isoformat()).encode()
    request = Request(
        "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
        data=body,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://web.cbr.ru/RuoniaXML",
        },
    )
    with urlopen(request, timeout=60) as response:
        payload = response.read()
    rows = []
    for node in ET.fromstring(payload).iter("ro"):
        day = date.fromisoformat(node.findtext("D0")[:10])
        rate = node.findtext("ruo")
        if rate:
            rows.append((day, Decimal(rate)))
    return rows