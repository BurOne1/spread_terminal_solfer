"""Пара BTC: вечный и кварталы Bybit в USDT, спот BTCUSDT.

Склейка — последняя пятница месяца контракта, 08:00 UTC.
spread_rate = raw − спот · EWMA14(8ч фандинг) · 3 · t.
"""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from market.bybit import BybitError, instruments
from market.models import Instrument, Pair
from market.roll import btc_quarter_points


class Command(BaseCommand):
    help = "Добавляет пару BTC и заводит её контракты Bybit."

    def add_arguments(self, parser):
        parser.add_argument("--years", type=int, default=1, help="сколько лет кварталов завести")

    def handle(self, *args, **options):
        pair, created = Pair.objects.update_or_create(
            code="BTC",
            defaults={
                "title": "биткоин",
                "future_root": "BTC",
                "perp_code": "BTCUSDT",
                "perp_symbol": "BTCUSDT",
                "spot_code": "BTCUSDT_SPOT",
                "spot_symbol": "BTCUSDT",
                "mic": "BYBIT",
                "foreign_source": "EWMA14",
                "venue": "bybit",
                "quote": "USDT",
                "lot": 1,
                "future_scale": 1,
                "active": True,
            },
        )
        mark = "создана" if created else "обновлена"
        self.stdout.write(f"пара BTC {mark}: USDT, склейка последняя пятница, ставка EWMA14 фандинга")
        Instrument.objects.update_or_create(
            code="BTCUSDT",
            defaults={
                "pair": pair, "kind": Instrument.Kind.PERP,
                "finam_symbol": "BTCUSDT", "lot": 1,
            },
        )
        Instrument.objects.update_or_create(
            code="BTCUSDT_SPOT",
            defaults={
                "pair": pair, "kind": Instrument.Kind.SPOT,
                "finam_symbol": "BTCUSDT", "lot": 1,
            },
        )
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=400)
        end = now + timedelta(days=370 * options["years"])
        rows = {item["symbol"]: item for item in btc_quarter_points("BTC", start, end)}
        try:
            for spec in instruments("linear", "BTC"):
                symbol = spec.get("symbol") or ""
                kind = spec.get("contractType") or ""
                if "Perpetual" in kind or symbol not in rows:
                    continue
                stamp = spec.get("deliveryTime")
                if stamp:
                    rows[symbol]["roll_at"] = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc)
        except BybitError as exc:
            self.stdout.write(f"instruments-info недоступен, даты по календарю: {exc}")
        ordered = sorted(rows.values(), key=lambda item: item["roll_at"])
        previous = None
        for row in ordered:
            listed = previous or row["listed_at"]
            instrument, made = Instrument.objects.update_or_create(
                code=row["code"],
                defaults={
                    "pair": pair,
                    "kind": Instrument.Kind.FUTURE,
                    "finam_symbol": row["symbol"],
                    "lot": 1,
                    "listed_at": listed,
                    "roll_at": row["roll_at"],
                },
            )
            self.stdout.write(
                f"  {'новый' if made else 'окно'} {instrument.code} "
                f"{listed:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d %H:%M}"
            )
            previous = row["roll_at"]
