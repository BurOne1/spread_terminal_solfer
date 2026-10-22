"""Грузит исторические часы из CSV в market_candle.

Колонки: code,ts,open,high,low,close,volume. code — CNYRUBF, CNYRUB_TOM, CRU2026.
Совпавший час обновляется, новый добавляется. Чужие инструменты не трогает.
"""

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from market.models import Candle, Instrument


class Command(BaseCommand):
    help = "Дописывает исторические свечи из CSV. Совпавший час заменяет."

    def add_arguments(self, parser):
        parser.add_argument("path", help="CSV с колонками code,ts,open,high,low,close,volume")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"нет файла {path}")
        instruments = {row.code: row for row in Instrument.objects.all()}
        grouped = {}
        skipped = 0
        with path.open(newline="") as handle:
            for record in csv.DictReader(handle):
                code = (record.get("code") or "").strip()
                instrument = instruments.get(code)
                if instrument is None:
                    skipped += 1
                    continue
                ts = datetime.fromisoformat(record["ts"])
                grouped.setdefault(instrument.id, []).append(Candle(
                    instrument=instrument,
                    ts=ts,
                    open=Decimal(record["open"]),
                    high=Decimal(record["high"]),
                    low=Decimal(record["low"]),
                    close=Decimal(record["close"]),
                    volume=Decimal(record.get("volume") or "0"),
                ))
        if not grouped:
            raise CommandError("ни одна строка не совпала с инструментом в базе")
        saved = 0
        with transaction.atomic():
            for rows in grouped.values():
                Candle.objects.bulk_create(
                    rows,
                    update_conflicts=True,
                    unique_fields=["instrument", "ts"],
                    update_fields=["open", "high", "low", "close", "volume"],
                )
                saved += len(rows)
        self.stdout.write(f"записано {saved}, пропущено без инструмента {skipped}")
        self.stdout.write("дальше: python manage.py build_spread")
