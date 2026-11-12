"""Проверяет и чинит кварталы Мосбиржи и масштаб цен пары.

1. Кварталы корня (Si → USD, Eu → EUR): заведённые у чужой пары переносятся
   к нужной, окно ставится по правилу склейки.
2. Масштаб: эталон — последние дни свечей пары (Finam, живой поток), кварталы
   делятся на future_scale. Свечи вечного, спота и кварталов, которые
   отличаются от эталона примерно в 1000 раз, приводятся к нему
   (market/units.py). Так чинится вечный из RUS_USDRUBF_1000 от TradingView.
3. Пересчитываются спред и аналитика затронутых пар и зависящих от них.

python manage.py fix_quarters --dry-run          # Si → USD и Eu → EUR, только показать
python manage.py fix_quarters                    # исправить
python manage.py fix_quarters --root CR --pair CNY
"""

import re
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from market.finam import future_symbol
from market.models import Candle, Instrument, Pair
from market.roll import quarter_points
from market.units import KINDS, bad_candles, fix_candles, instrument_scale, reference

DEFAULTS = (("Si", "USD"), ("Eu", "EUR"))


def _day(value):
    return f"{value:%Y-%m-%d}" if value else "—"


class Command(BaseCommand):
    help = "Проверяет кварталы Si/Eu (или --root) и масштаб цен пары. Чинит без --dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--root", default="", help="корень кварталов: Si, Eu, CR…; пусто — Si и Eu")
        parser.add_argument("--pair", default="", help="пара-владелец; по умолчанию Si → USD, Eu → EUR")
        parser.add_argument("--dry-run", action="store_true", help="только показать")

    def handle(self, *args, **options):
        if options["root"]:
            if not options["pair"]:
                raise CommandError("с --root укажите и --pair")
            jobs = [(options["root"], options["pair"])]
        else:
            jobs = DEFAULTS
        dry = options["dry_run"]
        touched = set()
        for root, code in jobs:
            owner = Pair.objects.filter(code=code).first()
            if owner is None:
                self.stdout.write(f"{root}: пары {code} нет, пропуск")
                continue
            with transaction.atomic():
                touched |= self.fix_owner(root, owner, dry)
                touched |= self.fix_units(owner, dry)
        if dry:
            self.stdout.write("проверка без записи")
        elif touched:
            self.rebuild(touched)
        else:
            self.stdout.write("исправлять нечего")

    def fix_owner(self, root, owner, dry):
        pattern = re.compile(rf"^{re.escape(root)}[HMUZ]20\d\d$", re.IGNORECASE)
        quarters = [
            item for item in Instrument.objects.filter(kind=Instrument.Kind.FUTURE).select_related("pair")
            if pattern.match(item.code)
        ]
        windows = {}
        for year in {int(item.code[-4:]) for item in quarters}:
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            for row in quarter_points(owner.future_root, start, start + timedelta(days=366)):
                windows[row["code"].upper()] = row
        self.stdout.write(f"{root} → {owner.code}: кварталов {len(quarters)}")
        touched = set()
        for item in sorted(quarters, key=lambda item: (item.roll_at is None, item.roll_at, item.code)):
            row = windows.get(item.code.upper())
            problems = []
            if item.pair_id != owner.id:
                problems.append(f"у пары {item.pair.code if item.pair else '—'}")
            if row and (item.roll_at != row["roll_at"] or item.listed_at != row["listed_at"]):
                problems.append(
                    f"окно {_day(item.listed_at)} → {_day(item.roll_at)}, "
                    f"нужно {_day(row['listed_at'])} → {_day(row['roll_at'])}"
                )
            if not problems:
                continue
            self.stdout.write(f"  {item.code}: " + "; ".join(problems))
            if dry:
                continue
            if item.pair is not None:
                touched.add(item.pair.code)
            updates = {"pair": owner, "lot": owner.lot}
            if row:
                updates.update(
                    listed_at=row["listed_at"], roll_at=row["roll_at"],
                    finam_symbol=item.finam_symbol or future_symbol(row["secid"]),
                )
            Instrument.objects.filter(pk=item.pk).update(**updates)
            touched.add(owner.code)
        return touched

    def fix_units(self, owner, dry):
        ref = reference(owner)
        if ref is None:
            self.stdout.write(f"{owner.code}: нет свежих свечей для эталона цены, масштаб не проверяется")
            return set()
        self.stdout.write(f"{owner.code}: эталон {ref:.4f} за единицу (последние дни свечей пары)")
        touched = set()
        for item in Instrument.objects.filter(pair=owner, kind__in=KINDS).order_by("kind", "roll_at", "code"):
            high, low = bad_candles(item, ref)
            if not high and not low:
                continue
            total = Candle.objects.filter(instrument=item).count()
            parts = []
            if high:
                parts.append(f"{high} ×1000 → разделить")
            if low:
                parts.append(f"{low} ÷1000 → умножить")
            self.stdout.write(
                f"  {item.code} (масштаб {instrument_scale(item)}): из {total} свечей " + ", ".join(parts)
            )
            if not dry:
                fix_candles(item, ref)
                touched.add(owner.code)
        return touched

    def rebuild(self, codes):
        from analytics.history import refresh
        from strategy.rebuild import _dependencies, rebuild_spread
        deps = _dependencies()
        codes = set(codes) | {follower for follower, needs in deps.items() if set(needs) & set(codes)}
        for code in sorted(codes):
            count = rebuild_spread(code)
            pair = Pair.objects.filter(code=code, active=True).first()
            history = refresh(pair) if pair else 0
            self.stdout.write(f"пересчитано {code}: спред {count}, аналитика {history}")
