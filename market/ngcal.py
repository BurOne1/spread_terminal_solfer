"""Календарь NG Мосбиржи как готовый инструмент: NGX6NGZ6@RTSX.

Мосбиржа торгует календарные спреды соседних месяцев газа отдельными
инструментами (тип SPREADS). Цена инструмента и есть спред, ног не нужно.
Окно — до последнего рабочего дня месяца первой ноги на Мосбирже: в этот
день пара переходит на следующий спред, NGX6NGZ6 → NGZ6NGF7 (30.11.2026).
Сам спред истекает с первой ногой на несколько дней раньше, до склейки
баров у окна просто нет. Уровни — абсолютные, в $/MMBtu.
"""

from datetime import datetime, timedelta, timezone

from market.expiry import moex_last_business_day
from market.models import Instrument
from strategy.params import ROLL_TIME

MONTHS = "FGHJKMNQUVXZ"
ROOT = "NG"


def _roll(day):
    hour, minute = ROLL_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def _secid(year, month):
    return f"{ROOT}{MONTHS[month - 1]}{year % 10}"


def windows(start, end):
    """Окна, задевающие [start, end): спред, его фронт, начало и склейка."""
    months = []
    for year in range(start.year - 1, end.year + 2):
        for month in range(1, 13):
            months.append((_roll(moex_last_business_day(year, month)), year, month))
    months.sort()
    covered = []
    for (opened, _y, _m), (expires, year, month) in zip(months, months[1:]):
        if not (opened < end and expires > start):
            continue
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        spread = f"{_secid(year, month)}{_secid(next_year, next_month)}"
        covered.append({
            "code": spread, "symbol": f"{spread}@RTSX",
            "front_code": f"{ROOT}{MONTHS[month - 1]}{year}", "front": f"{_secid(year, month)}@RTSX",
            "listed_at": opened, "roll_at": expires,
        })
    return covered


def ensure(pair, start, end, client=None, update=False):
    """Спреды окон в базе. Вернуть окна. client не нужен: склейка по календарю Мосбиржи."""
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
    return rows


def front_symbols(pair, now):
    """Котировка текущего окна. Недостающее окно заводится по правилу."""
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
    return [window.finam_symbol]
