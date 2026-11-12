"""Даты экспирации фьючерсов: из описания инструмента Finam, иначе по правилу биржи.

Finam отдаёт дату в /v1/assets/{symbol}: future_details.expiration_date
(время) или устаревшее expiration_date (год, месяц, день). Правила бирж —
запасной вариант для контрактов, которых Finam ещё не листинговал, и для
запуска без ключа. Праздники в правилах не учтены: ensure_contracts с ключом
поправит даты листингованных контрактов.
"""

from datetime import date, datetime, timedelta, timezone

from market.finam import FinamError

# Finam отдаёт экспирацию полночью по Москве: 2026-12-13T21:00:00Z — это 14 декабря.
MSK = timezone(timedelta(hours=3))


def _business_back(day, count):
    """День, на count будних дней раньше day."""
    while count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def third_wednesday(year, month):
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 14)


def cme_fx_expiry(year, month):
    """Валютные фьючерсы CME (6E, CNH): за два рабочих дня до третьей среды — обычно понедельник."""
    return _business_back(third_wednesday(year, month), 2)


def last_business_day(year, month):
    day = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# Нерабочие будни Мосбиржи в конце месяца: от них зависит склейка нефти и газа.
# Список дополняется по календарю биржи на следующий год.
MOEX_HOLIDAYS = {date(2024, 12, 31), date(2025, 12, 31), date(2026, 12, 31)}


def moex_last_business_day(year, month):
    """Последний рабочий день месяца на Мосбирже: будни без MOEX_HOLIDAYS."""
    day = last_business_day(year, month)
    while day.weekday() >= 5 or day in MOEX_HOLIDAYS:
        day -= timedelta(days=1)
    return day


def _parse(value):
    if isinstance(value, dict):
        if value.get("year"):
            return date(int(value["year"]), int(value.get("month") or 1), int(value.get("day") or 1))
        value = value.get("value") or value.get("seconds")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), MSK).date()
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment.astimezone(MSK).date() if moment.tzinfo else moment.date()
    return None


def finam_expiry(client, symbol):
    """Дата экспирации из Finam или None: нет ключа, нет инструмента, нет поля."""
    if client is None:
        return None
    try:
        body = client.asset(symbol)
    except (FinamError, OSError, ValueError):
        return None
    details = body.get("future_details") or body.get("futureDetails") or {}
    for value in (details.get("expiration_date"), details.get("expirationDate"),
                  body.get("expiration_date"), body.get("expirationDate")):
        found = _parse(value)
        if found:
            return found
    return None
