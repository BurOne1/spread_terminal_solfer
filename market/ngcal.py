"""Календарь NG Мосбиржи как готовый инструмент: NGX6NGZ6@RTSX.

Мосбиржа торгует календарные спреды соседних месяцев газа отдельными
инструментами (тип SPREADS). Цена инструмента и есть спред, ног считать
не нужно. Окно — до экспирации первой ноги: в этот день пара переходит
на следующий спред, NGX6NGZ6 → NGZ6NGF7. Дата — из Finam, иначе правило.

Фронт (NGX6@RTSX) — вторая нога окна: от его цены считаются уровни в процентах.
"""

from datetime import datetime, timedelta, timezone

from market.expiry import finam_expiry, moex_ng_expiry
from market.models import Instrument
from strategy.params import ROLL_TIME

MONTHS = "FGHJKMNQUVXZ"
ROOT = "NG"


def _roll(day):
    hour, minute = ROLL_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def _secid(year, month):
    return f"{ROOT}{MONTHS[month - 1]}{year % 10}"


def windows(start, end, expiries=None):
    """Окна, задевающие [start, end): спред, его фронт, начало и склейка.

    expiries — {символ фронта: дата} из Finam, иначе правило NG.
    """
    expiries = expiries or {}
    months = []
    for year in range(start.year - 1, end.year + 2):
        for month in range(1, 13):
            front = f"{_secid(year, month)}@RTSX"
            months.append((_roll(expiries.get(front) or moex_ng_expiry(year, month)), year, month))
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
    """Спреды и фронты окон в базе. client — Finam для точных дат. Вернуть окна."""
    expiries = {}
    if client is not None:
        for row in windows(start, end):
            found = finam_expiry(client, row["front"])
            if found:
                expiries[row["front"]] = found
    rows = windows(start, end, expiries)
    for row in rows:
        for code, symbol, kind in (
            (row["code"], row["symbol"], Instrument.Kind.FUTURE),
            (row["front_code"], row["front"], Instrument.Kind.FOREIGN),
        ):
            defaults = {
                "pair": pair, "kind": kind, "finam_symbol": symbol, "lot": pair.lot,
                "listed_at": row["listed_at"], "roll_at": row["roll_at"],
            }
            if update:
                Instrument.objects.update_or_create(code=code, defaults=defaults)
            else:
                Instrument.objects.get_or_create(code=code, defaults=defaults)
    return rows


def front_symbols(pair, now):
    """Спред и фронт текущего окна. Недостающее окно заводится по правилу."""
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
    front = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FOREIGN, roll_at=window.roll_at).first()
    return [window.finam_symbol] + ([front.finam_symbol] if front else [])
