"""Загрузка свечей из CSV, выгруженных в TradingView («Экспорт данных графика»).

Официального API у TradingView нет, поэтому файлы выгружаются вручную: на
графике контракта меню → «Экспорт данных графика». Колонки: time (unix-секунды
или дата), open, high, low, close, Volume — как в gas_spreads.py.

Инструмент определяется по имени файла:
- код контракта с годом из четырёх цифр: «MOEX_DLY_CRZ2025, 60.csv» → CRZ2025;
- иначе код инструмента из базы, встречающийся в имени: CNYRUBF, GLDRUBF.

Контракт, которого в базе ещё нет (старые годы), заводится с окном склейки:
- квартал Мосбиржи — у пары с этим корнем (CR → CNY, Si → USD);
- NG — по бирже в имени файла (TradingView пишет её первой): «NYMEX_…NGG2021» —
  окно пары NGNYMEX, иначе нога Мосбиржи (NGCAL или NGNYMEX); коды у бирж одинаковые;
- BR и BZ — окна BRNYMEX; CNH и 6E — окна CRCNH и SI6E.

Часовой проект: свечи мельче часа сворачиваются в часовые, крупнее —
пропускаются с предупреждением. Совпавшие часы перезаписываются.

python manage.py import_tv ~/tv/                # все *.csv папки
python manage.py import_tv ~/tv/CRZ2025.csv --dry-run
Потом: python manage.py build_spread
"""

import csv
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from market.finam import future_symbol
from market.models import Candle, Instrument, Pair
from market.roll import quarter_points
from market.units import factor, instrument_scale, reference

CONTRACT = re.compile(r"([A-Za-z0-9]{1,5}?)([FGHJKMNQUVXZ])(20\d{2})")
MONTHS = "FGHJKMNQUVXZ"


def parse_time(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        stamp = float(text)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_bars(path):
    """Свечи файла, свёрнутые в часы: {час: [open, high, low, close, volume]}, и шаг исходных баров."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = {name.lower().strip(): name for name in reader.fieldnames or []}
        missing = [name for name in ("time", "open", "high", "low", "close") if name not in columns]
        if missing:
            raise CommandError(f"{path.name}: нет колонок {', '.join(missing)}")
        volume = columns.get("volume")
        rows = []
        for row in reader:
            ts = parse_time(row[columns["time"]])
            try:
                values = [Decimal(row[columns[name]]) for name in ("open", "high", "low", "close")]
                size = Decimal(row[volume]) if volume and row.get(volume) not in (None, "") else Decimal(0)
            except (InvalidOperation, TypeError):
                continue
            if ts is None or values[3] <= 0:
                continue
            rows.append((ts, values, size))
    rows.sort(key=lambda item: item[0])
    steps = [b[0] - a[0] for a, b in zip(rows, rows[1:]) if b[0] > a[0]]
    step = min(steps) if steps else timedelta(hours=1)
    hours = {}
    for ts, (opened, high, low, closed), size in rows:
        hour = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        bar = hours.get(hour)
        if bar is None:
            hours[hour] = [opened, high, low, closed, size]
        else:
            bar[1], bar[2], bar[3], bar[4] = max(bar[1], high), min(bar[2], low), closed, bar[4] + size
    return hours, step


def _owner(**filters):
    return Pair.objects.filter(active=True, **filters).first()


def quarter_owner(root):
    """Пара кварталов корня: та, у которой их уже больше всего (Si → USD, Eu → EUR),
    при равенстве — с большим future_scale. Никакого случайного «первого»."""
    pairs = Pair.objects.filter(future_root__iexact=root, base="").exclude(
        foreign_source__in=("CMEFX", "LISTED", "NYMEX", "NGNYM", "TRI", "CAL"))
    pairs = pairs.annotate(own=Count("instruments", filter=Q(instruments__kind=Instrument.Kind.FUTURE)))
    owner = pairs.order_by("-own", "-future_scale", "id").first()
    if owner is not None:
        return owner
    # GD и SV — свои кварталы только у треугольников TRIGD и TRISV.
    # TRIGL и TRIGDS кварталы берут чужие, вторые кварталы (base) — тоже.
    return Pair.objects.filter(future_root__iexact=root, base="", foreign_source="TRI").exclude(
        code__in=("TRIGL", "TRIGDS")).order_by("id").first()


def _quarter_maker(root, letter, year):
    """Квартал Мосбиржи у пары с этим корнем: CR → CNY, Si → USD."""
    owner = quarter_owner(root)
    if owner is None or letter not in "HMUZ":
        return None, f"{root}{letter}{year}", ""
    code = f"{owner.future_root}{letter}{year}"

    def make():
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        for row in quarter_points(owner.future_root, start, start + timedelta(days=366)):
            if row["code"] == code:
                Instrument.objects.get_or_create(code=code, defaults={
                    "pair": owner, "kind": Instrument.Kind.FUTURE, "finam_symbol": future_symbol(row["secid"]),
                    "lot": owner.lot, "listed_at": row["listed_at"], "roll_at": row["roll_at"],
                })
    return make, code, owner.code


def _around(year, month):
    """Диапазон, задевающий окно месяца: окна строятся с соседями."""
    middle = datetime(year, month, 15, tzinfo=timezone.utc)
    return middle - timedelta(days=60), middle + timedelta(days=60)


def _monthly_maker(root, letter, year, nymex):
    """Месячные NG и Brent, CME-кварталы кросс-пар: (функция создания, код инструмента, пара)."""
    from market import brent, cross, ngnymex
    month = MONTHS.index(letter) + 1
    if root.upper() == "NG":
        if nymex:
            pair = _owner(foreign_source="NGNYM")
            moex_year, moex_month = (year - 1, 12) if month == 1 else (year, month - 1)
            code = ngnymex.legs(moex_year, moex_month)[0]
            return (lambda: ngnymex.ensure(pair, *_around(moex_year, moex_month))) if pair else None, code, pair.code if pair else ""
        # Нога Мосбиржи нужна только NGNYMEX: календарь NGCAL — готовая котировка.
        pair = _owner(foreign_source="NGNYM")
        return (lambda: ngnymex.ensure(pair, *_around(year, month))) if pair else None, f"NG{letter}{year}", pair.code if pair else ""
    if root.upper() in ("BR", "BZ"):
        pair = _owner(foreign_source="NYMEX")
        if pair is None:
            return None, f"{root}{letter}{year}", ""
        code = f"{root.upper()}{letter}{year}"

        def make():
            for row in brent.windows(*_around(year, month)):
                if code in (row["code"], row["nymex_code"]):
                    brent.ensure_window(pair, row)
        return make, code, pair.code
    for pair_code, spec in cross.CROSS.items():
        if root.upper() == spec["b"].upper():
            pair = _owner(code=pair_code)
            code = f"{spec['b']}{letter}{year}"
            return (lambda: cross.ensure(pair, *_around(year, month))) if pair else None, code, pair_code if pair else ""
    return None, None, ""


def find_instrument(name):
    """По имени файла: (инструмент или None, код, функция создания или None, пара-владелец)."""
    found = CONTRACT.search(name)
    if not found:
        upper = name.upper()
        for code in sorted(Instrument.objects.values_list("code", flat=True), key=len, reverse=True):
            if code.upper() in upper:
                return Instrument.objects.get(code=code), code, None, ""
        return None, "", None, ""
    root, letter, year = found.group(1), found.group(2), int(found.group(3))
    nymex = "NYMEX" in name.upper() or "XNYM" in name.upper()
    make, code, owner = _monthly_maker(root, letter, year, nymex)
    if code is None:
        make, code, owner = _quarter_maker(root, letter, year)
    instrument = Instrument.objects.filter(code__iexact=code).first()
    return instrument, code, (None if instrument else make), owner


def rescale(instrument, hours):
    """Файл в другом масштабе (RUS_USDRUBF_1000 — вечный ×1000): привести к эталону пары.

    Вернуть пометку для вывода или пустую строку. hours правится на месте.
    """
    if instrument is None:
        return ""
    closes = sorted(bar[3] for bar in hours.values())
    mult = factor(closes[len(closes) // 2], reference(instrument.pair), instrument_scale(instrument))
    if mult == 1:
        return ""
    for bar in hours.values():
        for index in range(4):
            bar[index] = bar[index] * mult
    return ", цены ÷1000 (в файле ×1000)" if mult < 1 else ", цены ×1000 (в файле ÷1000)"


def _window(instrument):
    """Окно склейки инструмента для вывода."""
    if instrument is None or instrument.roll_at is None:
        return "окна нет"
    opened = f"{instrument.listed_at:%Y-%m-%d}" if instrument.listed_at else "…"
    return f"окно {opened} → {instrument.roll_at:%Y-%m-%d}"


class Command(BaseCommand):
    help = "Загружает часовые свечи из CSV-выгрузок TradingView; инструмент — по имени файла."

    def add_arguments(self, parser):
        parser.add_argument("path", help="CSV-файл или папка с *.csv")
        parser.add_argument("--dry-run", action="store_true", help="только показать, что и куда загрузится")

    def handle(self, *args, **options):
        path = Path(options["path"]).expanduser()
        files = sorted(path.glob("*.csv")) if path.is_dir() else [path]
        if not files or not files[0].exists():
            raise CommandError(f"нет CSV: {path}")
        dry = options["dry_run"]
        total = 0
        for file in files:
            instrument, code, make, owner = find_instrument(file.stem)
            if instrument is None and make is None:
                hint = f" (код {code}: нет такого инструмента и пары, у которой его завести)" if code else ""
                self.stdout.write(f"{file.name}: инструмент не найден{hint}, пропуск")
                continue
            hours, step = read_bars(file)
            if step > timedelta(hours=1):
                self.stdout.write(f"{file.name}: шаг свечей {step}, нужен час или меньше — пропуск")
                continue
            if not hours:
                self.stdout.write(f"{file.name}: пустой, пропуск")
                continue
            first, last = min(hours), max(hours)
            if instrument is None:
                if dry:
                    # Завести и откатить: окно будет ровно таким, как при записи.
                    with transaction.atomic():
                        make()
                        planned = Instrument.objects.filter(code__iexact=code).first()
                        window = _window(planned)
                        note = rescale(planned, hours)
                        transaction.set_rollback(True)
                    self.stdout.write(
                        f"{file.name} → {code} (будет заведён у {owner}): {len(hours)} ч, "
                        f"данные {first:%Y-%m-%d} → {last:%Y-%m-%d}, {window}{note}"
                    )
                    continue
                make()
                instrument = Instrument.objects.filter(code__iexact=code).first()
                if instrument is None:
                    self.stdout.write(f"{file.name}: не удалось завести {code}, пропуск")
                    continue
            note = rescale(instrument, hours)
            self.stdout.write(
                f"{file.name} → {instrument.code} ({instrument.pair.code if instrument.pair else '—'}): "
                f"{len(hours)} ч, данные {first:%Y-%m-%d} → {last:%Y-%m-%d}, {_window(instrument)}{note}"
            )
            if dry:
                continue
            Candle.objects.bulk_create(
                [Candle(instrument=instrument, ts=ts, open=bar[0], high=bar[1], low=bar[2], close=bar[3], volume=bar[4])
                 for ts, bar in hours.items()],
                update_conflicts=True, unique_fields=["instrument", "ts"],
                update_fields=["open", "high", "low", "close", "volume"], batch_size=1000,
            )
            total += len(hours)
        self.stdout.write(f"итого {total} часов. Дальше: python manage.py build_spread" if not dry else "проверка без записи")
