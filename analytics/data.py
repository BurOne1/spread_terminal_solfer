"""Ряды контрактов пары из strategy_spreadbar: по дням или по шагам 4/8/16 часов.

Контракт — окно пары (SpreadBar.contract): квартал, месяц, кварталы CME у
кросс-пар, календарь NG. День — среднее часов дня (UTC), если торговых
часов не меньше MIN_BARS_DAY: в праздник одна из ног почти не торгует,
и устаревшие цены дают ложный спред. Ось — days_left: дни до склейки окна.

История длиннее окна торговли: спред контракта пересчитывается из свечей
за два месяца до склейки у месячных (нефть, газ) и за два квартала у
квартальных; хранится в analytics_historybar (analytics/history.py).
Свечей нет — берутся бары окна из strategy_spreadbar.

Часовые шаги: ось — часов до склейки контракта (roll_at) // шаг, значение —
среднее часовых баров шага. Вейвлет (analytics.denoise) сглаживает часовой
ряд контракта до того, как он делится на дни или шаги.

Фандинг — дневной ряд вечного (strategy.runner.funding_of) на единицу
актива, разложенный по окнам контрактов: день d — склейка − d дней.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from analytics.denoise import wavelet_denoise
from analytics.history import refresh, stale
from analytics.models import HistoryBar
from strategy.models import SpreadBar

MONTHS = "FGHJKMNQUVXZ"
MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь"]
MIN_BARS_DAY = 3
LONG_CODE = re.compile(r"([FGHJKMNQUVXZ])(\d{4})(?:[.\-]\w+)?$")
SHORT_CODE = re.compile(r"^[A-Za-z0-9]*?[A-Za-z]([FGHJKMNQUVXZ])(\d)")


def contract_month(code, roll_at):
    """(буква месяца, год) контракта: CRZ2026 → Z 2026, NGX6NGZ6 → X 2026 (десятилетие — по склейке)."""
    found = LONG_CODE.search(code)
    if found:
        return found.group(1), int(found.group(2))
    found = SHORT_CODE.search(code)
    if found and roll_at is not None:
        digit = int(found.group(2))
        year = roll_at.year - roll_at.year % 10 + digit
        if year > roll_at.year + 1:
            year -= 10
        return found.group(1), year
    if roll_at is not None:
        return MONTHS[roll_at.month - 1], roll_at.year
    return "", 0


def _rows(pair, series):
    """(ts, контракт, код, склейка, days_left, значение, цена): таблица истории или бары окна."""
    if stale(pair):
        refresh(pair)
    field = "spread_rate" if series == "rate" else "raw_spread"
    columns = ("ts", "contract_id", "contract__code", "contract__roll_at", "days_left", field, "spot")
    history = HistoryBar.objects.filter(pair=pair)
    if history.exists():
        return history.order_by("ts").values_list(*columns)
    return SpreadBar.objects.filter(pair=pair).order_by("ts").values_list(*columns)


def load(pair, series="raw"):
    """Часовые ряды контрактов: {контракт: (код, склейка, [(ts, days_left, значение, цена)])} по времени."""
    found = {}
    for ts, contract_id, code, roll_at, days_left, value, spot in _rows(pair, series):
        item = found.setdefault(contract_id, (code, roll_at, []))
        if value is not None and spot is not None:
            item[2].append((ts, days_left, float(value), float(spot)))
    return found


def _daily(bars):
    """Часовые бары → [(days_left, среднее, цена)]: день — не меньше MIN_BARS_DAY баров."""
    days = {}
    for ts, days_left, value, spot in bars:
        bucket = days.setdefault(ts.date(), [0, 0.0, 0.0, days_left])
        bucket[0] += 1
        bucket[1] += value
        bucket[2] += spot
        bucket[3] = min(bucket[3], days_left)
    points = sorted(
        (left, total / count, price / count)
        for count, total, price, left in days.values()
        if count >= MIN_BARS_DAY
    )
    # Два дня с одним days_left (выходные внутри окна не дают сдвига) — берём последний.
    unique = {}
    for left, value, price in points:
        unique[left] = (left, value, price)
    return sorted(unique.values())


def _stepped(bars, roll_at, step_hours):
    """Часовые бары → [(шаг до склейки, среднее, цена)], шаг — (склейка − время) // step_hours."""
    buckets = defaultdict(lambda: [0, 0.0, 0.0])
    for ts, days_left, value, spot in bars:
        if roll_at is not None:
            hours = (roll_at - ts).total_seconds() / 3600.0
        else:
            hours = days_left * 24.0
        if hours < 0:
            continue
        bucket = buckets[int(hours // step_hours)]
        bucket[0] += 1
        bucket[1] += value
        bucket[2] += spot
    return sorted((index, total / count, price / count) for index, (count, total, price) in buckets.items())


def contracts(pair, series="raw", now=None, step_hours=24, denoise=False, loaded=None):
    """Контракты пары по порядку: код, месяц, год, идёт ли, точки (шаг, значение, цена).

    step_hours=24 — дневные точки по days_left, как раньше; 4/8/16 — шаги часов до склейки.
    denoise — часовой ряд контракта сначала сглаживается вейвлетом. loaded — готовый load().
    """
    now = now or datetime.now(timezone.utc)
    meta = loaded if loaded is not None else load(pair, series)
    found = []
    for contract_id, (code, roll_at, bars) in meta.items():
        if denoise and bars:
            smooth = wavelet_denoise([bar[2] for bar in bars])
            bars = [(ts, left, value, spot) for (ts, left, _old, spot), value in zip(bars, smooth)]
        points = _daily(bars) if step_hours >= 24 else _stepped(bars, roll_at, step_hours)
        letter, year = contract_month(code, roll_at)
        found.append({
            "id": contract_id, "code": code, "month": letter, "year": year, "roll_at": roll_at,
            "live": roll_at is None or roll_at > now,
            "points": points,
        })
    found.sort(key=lambda item: (item["year"], MONTHS.index(item["month"]) if item["month"] in MONTHS else 0, item["code"]))
    return found


def funding_daily(pair, denoise=False):
    """Фандинг вечного пары по дням: ({дата: значение}, режим) или (None, "").

    daily — деньги на контракт за день → на единицу актива (делим на номинал контракта);
    interval (Bybit) — сумма 8-часовых ставок за день в долях: на единицу — × цена.
    """
    from strategy.runner import funding_of, notional_of
    rows, mode = funding_of(pair)
    if not rows:
        return None, ""
    by_day = defaultdict(float)
    if mode == "interval":
        for ts, rate in rows:
            by_day[ts.date() if hasattr(ts, "date") else ts] += rate
    else:
        unit = float(notional_of(pair))
        for day, value in rows:
            by_day[day] += value / unit
    days = sorted(by_day)
    values = [by_day[day] for day in days]
    if denoise:
        values = wavelet_denoise(values)
    return dict(zip(days, values)), mode


def funding_matrix(pair, used, width, denoise=False, tolerance=3):
    """F и P «контракт × день до склейки 0 … width−1» для core.funding_grid или None, если фандинга нет.

    used — контракты с дневными точками (contracts(..., step_hours=24)): из них цена
    для капитала. День до первой и после последней записи фандинга — nan.
    """
    from analytics.core import value_at
    series, mode = funding_daily(pair, denoise)
    if not series:
        return None
    first, last = min(series), max(series)
    F, P = [], []
    for item in used:
        roll = item["roll_at"]
        if roll is None:
            continue
        prices = {day: price for day, _value, price in item["points"]}
        frow, prow = [], []
        for day in range(width):
            date = roll.date() - timedelta(days=day)
            price = value_at(prices, day, tolerance)
            prow.append(price)
            if date < first or date > last:
                frow.append(float("nan"))
                continue
            value = series.get(date, 0.0)
            frow.append(value * price if mode == "interval" else value)
        F.append(frow)
        P.append(prow)
    if not F:
        return None
    return np.array(F, dtype=float), np.array(P, dtype=float)
