"""Склейка квартала: третья четверг месяца H/M/U/Z, 20:50 UTC.

Окно контракта — от склейки предыдущего до своей, не включая правый край.
CRU2026 начинается в день склейки CRH2026.
"""

from datetime import date, datetime, timedelta, timezone

from strategy.params import MONTH_CODE, ROLL_TIME


def third_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 14)


def roll_at(letter: str, year: int) -> datetime:
    day = third_thursday(year, MONTH_CODE[letter])
    hour, minute = ROLL_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def contract_code(root: str, letter: str, year: int) -> str:
    return f"{root}{letter}{year}"


def moex_secid(root: str, letter: str, year: int) -> str:
    """Короткий код Мосбиржи: CRH6. После 2029 одна цифра года снова начнёт повторяться."""
    return f"{root}{letter}{year % 10}"


def quarter_points(root: str, start: datetime, end: datetime):
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    points = []
    for year in range(start.year - 1, end.year + 2):
        for letter in "HMUZ":
            points.append((roll_at(letter, year), letter, year))
    points.sort()
    covered = []
    for index in range(1, len(points)):
        opened, expires, letter, year = points[index - 1][0], points[index][0], points[index][1], points[index][2]
        if opened < end and expires > start:
            covered.append({
                "code": contract_code(root, letter, year),
                "secid": moex_secid(root, letter, year),
                "listed_at": opened,
                "roll_at": expires,
            })
    return covered


def last_friday(year: int, month: int) -> date:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 4) % 7)


def btc_roll_at(year: int, month: int) -> datetime:
    day = last_friday(year, month)
    return datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc)


BYBIT_MONTH = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def bybit_symbol(root: str, roll: datetime) -> str:
    return f"{root}USDT-{roll.day:02d}{BYBIT_MONTH[roll.month - 1]}{roll.year % 100:02d}"


def btc_quarter_points(root: str, start: datetime, end: datetime):
    """Окно квартала Bybit: от последней пятницы предыдущего до своей, 08:00 UTC."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    points = []
    for year in range(start.year - 1, end.year + 2):
        for month in (3, 6, 9, 12):
            points.append(btc_roll_at(year, month))
    points.sort()
    covered = []
    for index in range(1, len(points)):
        opened, expires = points[index - 1], points[index]
        if opened < end and expires > start:
            covered.append({
                "code": bybit_symbol(root, expires),
                "symbol": bybit_symbol(root, expires),
                "listed_at": opened,
                "roll_at": expires,
            })
    return covered