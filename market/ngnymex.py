"""Газ: NG Мосбиржи минус NG NYMEX (Henry Hub), $/MMBtu.

NG Мосбиржи месяца M рассчитывается по NYMEX месяца M+1: NYMEX на месяц
впереди, NGZ6@RTSX (декабрь) ↔ NGF27@XNYM (январь). Справедливое значение — ноль.

Окна считаются по Мосбирже: склейка — последний рабочий день месяца контракта
MOEX, ROLL_TIME UTC (NGX2026 → 30.11.2026). Обе ноги истекают на несколько дней
раньше (за три рабочих дня до первого числа месяца NYMEX); после этого у окна
нет общих баров, и спред просто не строится до склейки.

Окно — инструмент пары с кодом месяца Мосбиржи и пометкой -NYM
(NGZ2026-NYM, символ NGF27@XNYM): коды NGZ2026 уже заняты фронтами
календаря NGCAL. Ноги Мосбиржи (NGZ2026, NGZ6@RTSX) принадлежат этой паре,
вид FOREIGN, окно то же; календарь NGCAL находит в них свой фронт по символу.
"""

from datetime import datetime, timedelta, timezone

from market.expiry import moex_last_business_day
from market.models import Instrument
from strategy.params import ROLL_TIME

MONTHS = "FGHJKMNQUVXZ"
ROOT = "NG"
SUFFIX = "-NYM"


def _roll(day):
    hour, minute = ROLL_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def legs(year, month):
    """MOEX месяца (year, month) → (код окна, символ MOEX, символ NYMEX месяцем позже)."""
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    letter = MONTHS[month - 1]
    return (
        f"{ROOT}{letter}{year}{SUFFIX}",
        f"{ROOT}{letter}{year % 10}@RTSX",
        f"{ROOT}{MONTHS[next_month - 1]}{next_year % 100:02d}@XNYM",
    )


def parse_code(code):
    """NGZ2026-NYM → (2026, 12)."""
    body = code[len(ROOT):-len(SUFFIX)]
    return int(body[1:]), MONTHS.index(body[0]) + 1


def windows(start, end):
    """Окна, задевающие [start, end): от склейки предыдущего месяца MOEX до своей."""
    points = []
    for year in range(start.year - 1, end.year + 2):
        for month in range(1, 13):
            points.append((_roll(moex_last_business_day(year, month)), year, month))
    points.sort()
    covered = []
    for (opened, _y, _m), (expires, year, month) in zip(points, points[1:]):
        if opened < end and expires > start:
            code, moex, nymex = legs(year, month)
            covered.append({"code": code, "moex": moex, "symbol": nymex, "listed_at": opened, "roll_at": expires})
    return covered


def ensure(pair, start, end, client=None, update=False):
    """Окна (ноги NYMEX) и ноги Мосбиржи в базе. Вернуть окна.

    client не нужен: склейка по календарю Мосбиржи, а не по экспирации.
    """
    rows = windows(start, end)
    for row in rows:
        defaults = {
            "pair": pair, "kind": Instrument.Kind.FUTURE, "finam_symbol": row["symbol"], "lot": pair.lot,
            "listed_at": row["listed_at"], "roll_at": row["roll_at"],
        }
        if update:
            Instrument.objects.update_or_create(code=row["code"], defaults=defaults)
        else:
            Instrument.objects.get_or_create(code=row["code"], defaults=defaults)
        moex_leg(pair, row)
    return rows


def moex_code(code):
    """Код ноги Мосбиржи окна: NGZ2026-NYM → NGZ2026."""
    return code[:-len(SUFFIX)] if code.endswith(SUFFIX) else code


def moex_leg(pair, row):
    """Нога Мосбиржи окна: у этой пары, вид FOREIGN, окно как у окна NYMEX.

    Ищется по коду (NGZ2026), а не по символу: NGZ6@RTSX бывает раз в десять лет.
    """
    code = moex_code(row["code"])
    leg = Instrument.objects.filter(code=code).first()
    fields = {
        "pair": pair, "kind": Instrument.Kind.FOREIGN, "finam_symbol": row["moex"], "lot": pair.lot,
        "listed_at": row["listed_at"], "roll_at": row["roll_at"],
    }
    if leg is None:
        return Instrument.objects.create(code=code, **fields)
    Instrument.objects.filter(pk=leg.pk).update(**fields)
    return leg


def front_symbols(pair, now):
    """NYMEX и MOEX текущего окна. Недостающее окно заводится по правилу."""
    def current():
        return (
            Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__gt=now)
            .order_by("roll_at").first()
        )
    window = current()
    if window is None:
        ensure(pair, now - timedelta(days=1), now + timedelta(days=120))
        window = current()
    if window is None:
        return []
    _code, moex, nymex = legs(*parse_code(window.code))
    return [nymex, moex]
