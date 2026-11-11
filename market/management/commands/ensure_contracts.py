"""Создаёт недостающие инструменты пары, свечи не трогает.

Окно квартала: listed_at — склейка предыдущего, roll_at — своя.
CRU2026 начинается в день склейки CRH2026.
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from market import cross, ngcal
from market.brent import ensure_window
from market.brent import windows as brent_windows
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
            if pair.calendar:
                self.stdout.write(f"{pair.code}: календарь, кварталы пары {pair.base}")
                continue
            if pair.nymex:
                self.stdout.write(f"{pair.code}: месячные BR и BZ")
                for row in brent_windows(start, end):
                    instrument, created = ensure_window(pair, row, update=True)
                    mark = "новый" if created else "окно"
                    self.stdout.write(
                        f"  {mark} {instrument.code} / {row['nymex_code']} "
                        f"{row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}"
                    )
                continue
            if pair.cross or pair.listed:
                module = cross if pair.cross else ngcal
                client = _client()
                try:
                    rows = module.ensure(pair, start, end, client=client, update=True)
                finally:
                    if client is not None:
                        client.close()
                source = "даты из Finam" if client is not None else "даты по правилу биржи: нет ключа Finam"
                self.stdout.write(f"{pair.code}: окон {len(rows)}, {source}")
                for row in rows:
                    self.stdout.write(f"  {row['code']} {row['listed_at']:%Y-%m-%d} → {row['roll_at']:%Y-%m-%d}")
                continue
            if pair.foreign_source == "TRI":
                if pair.code == "TRIGDS":
                    self.stdout.write("TRIGDS: контракты чужие, GD из TRIGD, SI из USD, спот из GOLD")
                    continue
                if pair.base:
                    self.stdout.write(f"{pair.code}: вторые кварталы ног {pair.base}")
                    continue
                if pair.code == "TRIGL":
                    self.stdout.write("TRIGL: контракты чужие, GD из TRIGD, SI из USD, GL из GOLD")
                    continue
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


def _client():
    """Finam для точных дат экспирации CME и NG; без ключа — правило биржи."""
    if not settings.FINAM_API_SECRET:
        return None
    from market.finam import FinamClient
    return FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL)
