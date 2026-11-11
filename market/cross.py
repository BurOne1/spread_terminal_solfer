"""Кросс-курсы через CME: CR × CNH × 1000 − Si и Si × 6E − Eu, в пунктах Si и Eu.

Торгуются две ноги Мосбиржи (CR и Si, Si и Eu), третья — валютный фьючерс
CME (CNH — юани за доллар, 6E — доллары за евро), он переводит одну ногу в
валюту другой. Справедливое значение — ноль: форварды одного курса.
Отклонение — разница офшорного юаня и биржевого, курсов Мосбиржи и мира.

Окно пары задаёт квартал CME: он истекает за два рабочих дня до третьей
среды, на три дня раньше Мосбиржи. В этот день все три ноги переходят на
следующий квартал. Окна — инструменты пары (код CNHZ2026, символ
CNHZ26@XCME), ноги Мосбиржи — её кварталы того же месяца (CRZ6@RTSX).
"""

from datetime import datetime, timedelta, timezone

from market.expiry import cme_fx_expiry, finam_expiry
from market.models import Instrument

CME_MIC = "XCME"
QUARTERS = {"H": 3, "M": 6, "U": 9, "Z": 12}
# CME валютные фьючерсы перестают торговаться в 9:16 по Чикаго — около 14–15 UTC.
ROLL_HOUR = 14

CROSS = {
    "CRCNH": {
        "title": "CR × CNH − Si", "a": "CR", "b": "CNH", "c": "Si", "mult": 1000,
        "deps": ("CNY", "USD"), "note": "рубли за 1000 долларов, пункты Si",
    },
    "SI6E": {
        "title": "Si × 6E − Eu", "a": "Si", "b": "6E", "c": "Eu", "mult": 1,
        "deps": ("USD", "EUR"), "note": "рубли за 1000 евро, пункты Eu",
    },
}


def _roll(day):
    return datetime(day.year, day.month, day.day, ROLL_HOUR, 0, tzinfo=timezone.utc)


def parse_code(spec, code):
    """CNHZ2026 → ("Z", 2026)."""
    tail = code[len(spec["b"]):]
    return tail[0], int(tail[1:])


def legs(spec, letter, year):
    """Символы трёх ног квартала: (a, b, c)."""
    return (
        f"{spec['a']}{letter}{year % 10}@RTSX",
        f"{spec['b']}{letter}{year % 100:02d}@{CME_MIC}",
        f"{spec['c']}{letter}{year % 10}@RTSX",
    )


def windows(spec, start, end, expiries=None):
    """Окна, задевающие [start, end): код окна, символы ног, начало и склейка.

    expiries — {символ CME: дата} из Finam, иначе правило CME.
    """
    expiries = expiries or {}
    points = []
    for year in range(start.year - 1, end.year + 2):
        for letter, month in QUARTERS.items():
            symbol = legs(spec, letter, year)[1]
            points.append((_roll(expiries.get(symbol) or cme_fx_expiry(year, month)), letter, year))
    points.sort()
    covered = []
    for (opened, _letter, _year), (expires, letter, year) in zip(points, points[1:]):
        if opened < end and expires > start:
            a, b, c = legs(spec, letter, year)
            covered.append({
                "code": f"{spec['b']}{letter}{year}", "symbol": b, "a": a, "c": c,
                "listed_at": opened, "roll_at": expires,
            })
    return covered


def moex_leg(pair, symbol):
    """Квартал Мосбиржи по символу: чужой (пары USD, CNY, EUR) или свой новый."""
    found = Instrument.objects.filter(finam_symbol=symbol).first()
    if found is not None:
        return found
    secid = symbol.split("@")[0]
    root, letter, digit = secid[:-2], secid[-2], int(secid[-1])
    now_year = datetime.now(timezone.utc).year
    year = now_year - now_year % 10 + digit
    if year < now_year - 1:
        year += 10
    code = f"{root}{letter}{year}"
    if Instrument.objects.filter(code=code).exists():
        code = f"{code}.{pair.code}"
    return Instrument.objects.create(
        pair=pair, code=code, finam_symbol=symbol, kind=Instrument.Kind.FOREIGN, lot=1,
    )


def ensure(pair, start, end, client=None, update=False):
    """Окна и ноги в базе. client — Finam для точных дат CME. Вернуть окна."""
    spec = CROSS[pair.code]
    expiries = {}
    if client is not None:
        for row in windows(spec, start, end):
            found = finam_expiry(client, row["symbol"])
            if found:
                expiries[row["symbol"]] = found
    rows = windows(spec, start, end, expiries)
    for row in rows:
        defaults = {
            "pair": pair, "kind": Instrument.Kind.FUTURE, "finam_symbol": row["symbol"], "lot": 1,
            "listed_at": row["listed_at"], "roll_at": row["roll_at"],
        }
        if update:
            Instrument.objects.update_or_create(code=row["code"], defaults=defaults)
        else:
            Instrument.objects.get_or_create(code=row["code"], defaults=defaults)
        moex_leg(pair, row["a"])
        moex_leg(pair, row["c"])
    return rows


def front_symbols(pair, now):
    """Три символа текущего окна. Недостающее окно заводится по правилу CME."""
    window = (
        Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__gt=now)
        .order_by("roll_at").first()
    )
    if window is None:
        ensure(pair, now - timedelta(days=1), now + timedelta(days=200))
        window = (
            Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__gt=now)
            .order_by("roll_at").first()
        )
    if window is None:
        return []
    spec = CROSS[pair.code]
    a, b, c = legs(spec, *parse_code(spec, window.code))
    return [b, a, c]
