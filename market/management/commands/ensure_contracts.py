"""Создаёт недостающие инструменты пары, свечи не трогает.

Окно квартала: listed_at — склейка предыдущего, roll_at — своя.
CRU2026 начинается в день склейки CRH2026.
"""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from market.finam import future_symbol
from market.models import Instrument, Pair
from market.roll import quarter_points


class Command(BaseCommand):
    help = "Создаёт недостающие кварталы, вечный и спот. Свечи не трогает."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")
        parser.add_argument("--years", type=int, default=2, help="сколько лет вперёд завести кварталы")

    def handle(self, *args, **options):
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=400)
        end = now + timedelta(days=370 * options["years"])
        pairs = Pair.objects.filter(active=True)
        if options["pair"]:
            pairs = pairs.filter(code=options["pair"])
        if not pairs:
            self.stdout.write("активных пар нет")
            return
        for pair in pairs:
            if pair.venue == "bybit":
                self.stdout.write(f"{pair.code}: Bybit, контракты заводит add_btc")
                continue
            if pair.foreign_source == "TRI":
                self.stdout.write(f"{pair.code}: треугольник, свои только кварталы {pair.future_root}")
                for row in quarter_points(pair.future_root, start, end):
                    instrument, created = Instrument.objects.update_or_create(
                        code=row["code"],
                        defaults={
                            "pair": pair,
                            "kind": Instrument.Kind.FUTURE,
                            "finam_symbol": future_symbol(row["secid"]),
                            "lot": pair.lot,
                            "listed_at": row["listed_at"],
                            "roll_at": row["roll_at"],
                        },
                    )
                    mark = "новый" if created else "окно"
                    self.stdout.write(
                        f"  {mark} {instrument.code} {row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}"
                    )
                continue
            perp, _ = Instrument.objects.update_or_create(
                code=pair.perp_code,
                defaults={
                    "pair": pair,
                    "kind": Instrument.Kind.PERP,
                    "finam_symbol": pair.perp_symbol,
                    "lot": pair.lot,
                },
            )
            if pair.spot_code == pair.perp_code:
                self.stdout.write(f"{pair.code}: {perp.code}, спот = вечный")
            else:
                spot, _ = Instrument.objects.update_or_create(
                    code=pair.spot_code,
                    defaults={
                        "pair": pair,
                        "kind": Instrument.Kind.SPOT,
                        "finam_symbol": pair.spot_symbol,
                        "lot": pair.lot,
                    },
                )
                self.stdout.write(f"{pair.code}: {perp.code}, {spot.code}")
            for row in quarter_points(pair.future_root, start, end):
                instrument, created = Instrument.objects.update_or_create(
                    code=row["code"],
                    defaults={
                        "pair": pair,
                        "kind": Instrument.Kind.FUTURE,
                        "finam_symbol": future_symbol(row["secid"]),
                        "lot": pair.lot,
                        "listed_at": row["listed_at"],
                        "roll_at": row["roll_at"],
                    },
                )
                mark = "новый" if created else "окно"
                self.stdout.write(
                    f"  {mark} {instrument.code} {row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}"
                )
