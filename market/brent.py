"""Brent: месячный BR Мосбиржи против BZ на NYMEX (Brent Last Day Financial).

BZ рассчитывается по финальной цене ICE Brent того же месяца и истекает
в тот же день: последний рабочий день Лондона второго месяца перед
месяцем поставки. BZF2027 — 30.11.2026.
BR Мосбиржи исполняется по той же цене ICE, поэтому его месяц на один
раньше: BRZ2026 (декабрь) ↔ BZF2027 (январь). BR торгуется ещё день
после экспирации BZ, но второй ноги тогда уже нет, и пара переходит дальше.

Окно пары — от экспирации предыдущего BZ до своей, ROLL_TIME UTC.
Символы Финама: BRZ6@RTSX и BZF27@XNYM.
"""

from datetime import date, datetime, timedelta, timezone

from market.finam import future_symbol
from market.models import Instrument
from strategy.params import ROLL_TIME

MONTHS = "FGHJKMNQUVXZ"
MOEX_ROOT = "BR"
NYMEX_ROOT = "BZ"
NYMEX_MIC = "XNYM"


def easter(year: int) -> date:
    """Пасха по григорианскому календарю (алгоритм Гаусса в форме Meeus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _last_monday(year: int, month: int) -> date:
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=last.weekday())


def _observed(days):
    """Праздник в выходной переносится на ближайший свободный будний день."""
    taken = set()
    for day in sorted(days):
        while day.weekday() >= 5 or day in taken:
            day += timedelta(days=1)
        taken.add(day)
    return taken


def uk_holidays(year: int) -> set:
    """Банковские выходные Англии, от которых зависит конец месяца ICE.

    Пасха бывает в конце марта, весенний и летний — последние
    понедельники мая и августа. Разовые праздники биржа объявляет
    отдельно, их здесь нет.
    """
    sunday = easter(year)
    days = {sunday - timedelta(days=2), sunday + timedelta(days=1), _last_monday(year, 5), _last_monday(year, 8)}
    first_may = date(year, 5, 1)
    days.add(first_may + timedelta(days=(7 - first_may.weekday()) % 7))
    days |= _observed([date(year, 1, 1)])
    days |= _observed([date(year, 12, 25), date(year, 12, 26)])
    return days


def last_business_day(year: int, month: int) -> date:
    day = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    holidays = uk_holidays(year)
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


def expiry(year: int, month: int) -> date:
    """Последний день торгов BZ месяца поставки (year, month)."""
    month -= 2
    if month <= 0:
        month += 12
        year -= 1
    return last_business_day(year, month)


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
