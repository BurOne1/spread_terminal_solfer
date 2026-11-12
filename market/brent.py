"""Brent: месячный BR Мосбиржи против BZ на NYMEX (Brent Last Day Financial).

BZ рассчитывается по финальной цене ICE Brent того же месяца и истекает
в последний рабочий день Лондона второго месяца перед месяцем поставки.
BR Мосбиржи исполняется по той же цене ICE, поэтому NYMEX на месяц впереди:
BRZ2026 (декабрь) ↔ BZF2027 (январь). BR месяца M ближний весь месяц M−1
и истекает в первый рабочий день месяца M.

Окна считаются по Мосбирже: склейка — последний рабочий день Мосбиржи
месяца M−1, ROLL_TIME UTC (BRZ2026 → 30.11.2026, BRF2027 → 30.12.2026:
31.12 Мосбиржа не работает). Если Лондон закрыт раньше (банковский
выходной в конце месяца), BZ уже не торгуется, и до склейки баров нет.
Символы Финама: BRZ6@RTSX и BZF27@XNYM.
"""

from datetime import date, datetime, timedelta, timezone

from market.expiry import moex_last_business_day
from market.finam import future_symbol
from market.models import Instrument
from strategy.params import ROLL_TIME

MONTHS = "FGHJKMNQUVXZ"
MOEX_ROOT = "BR"
NYMEX_ROOT = "BZ"
NYMEX_MIC = "XNYM"


def expiry(year: int, month: int) -> date:
    """Склейка окна BZ месяца (year, month): последний рабочий день Мосбиржи
    месяца, в котором BR этого окна ближний (за два месяца до месяца BZ)."""
    month -= 2
    if month <= 0:
        month += 12
        year -= 1
    return moex_last_business_day(year, month)


def roll_at(year: int, month: int) -> datetime:
    day = expiry(year, month)
    hour, minute = ROLL_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def windows(start: datetime, end: datetime):
    """Окна пары, задевающие [start, end): BR и BZ одного окна и его границы."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    points = sorted(
        (roll_at(year, month), year, month)
        for year in range(start.year - 1, end.year + 2)
        for month in range(1, 13)
    )
    covered = []
    for (opened, _year, _month), (expires, year, month) in zip(points, points[1:]):
        if not (opened < end and expires > start):
            continue
        moex_year, moex_month = (year, month - 1) if month > 1 else (year - 1, 12)
        moex_letter, letter = MONTHS[moex_month - 1], MONTHS[month - 1]
        covered.append({
            "code": f"{MOEX_ROOT}{moex_letter}{moex_year}",
            "secid": f"{MOEX_ROOT}{moex_letter}{moex_year % 10}",
            "nymex_code": f"{NYMEX_ROOT}{letter}{year}",
            "nymex_secid": f"{NYMEX_ROOT}{letter}{year % 100:02d}",
            "listed_at": opened,
            "roll_at": expires,
        })
    return covered


def nymex_symbol(secid: str) -> str:
    return f"{secid}@{NYMEX_MIC}"


def ensure_window(pair, row, update=False):
    """BR и BZ окна в базе. update — переписать окно у уже заведённых. Вернуть (BR, создан)."""
    legs = (
        (row["code"], Instrument.Kind.FUTURE, future_symbol(row["secid"])),
        (row["nymex_code"], Instrument.Kind.FOREIGN, nymex_symbol(row["nymex_secid"])),
    )
    made = []
    for code, kind, symbol in legs:
        defaults = {
            "pair": pair,
            "kind": kind,
            "finam_symbol": symbol,
            "lot": pair.lot,
            "listed_at": row["listed_at"],
            "roll_at": row["roll_at"],
        }
        if update:
            made.append(Instrument.objects.update_or_create(code=code, defaults=defaults))
        else:
            made.append(Instrument.objects.get_or_create(code=code, defaults=defaults))
    return made[0]


def front_symbols(pair, now):
    """Символы текущего окна. Недостающие инструменты заводятся: после
    экспирации BZ следующий месяц уже есть, даже без ensure_contracts.
    """
    rows = [row for row in windows(now - timedelta(days=1), now + timedelta(days=70)) if row["roll_at"] > now][:1]
    for row in rows:
        ensure_window(pair, row)
    return [symbol for row in rows for symbol in (future_symbol(row["secid"]), nymex_symbol(row["nymex_secid"]))]
