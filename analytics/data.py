"""Дневные ряды контрактов пары из strategy_spreadbar.

Контракт — окно пары (SpreadBar.contract): квартал, месяц, кварталы CME у
кросс-пар, календарь NG. День — среднее часов дня (UTC), если торговых
часов не меньше MIN_BARS_DAY: в праздник одна из ног почти не торгует,
и устаревшие цены дают ложный спред. Ось — days_left: дни до склейки окна.

История длиннее окна торговли: спред контракта пересчитывается из свечей
за два месяца до склейки у месячных (нефть, газ) и за два квартала у
квартальных; хранится в analytics_historybar (analytics/history.py).
Свечей нет — берутся бары окна из strategy_spreadbar.
"""

import re
from datetime import datetime, timezone

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


def contracts(pair, series="raw", now=None):
    """Контракты пары по порядку: код, месяц, год, идёт ли, дневные точки (день, значение, цена)."""
    now = now or datetime.now(timezone.utc)
    rows = _rows(pair, series)
    days = {}
    meta = {}
    for ts, contract_id, code, roll_at, days_left, value, spot in rows:
        meta[contract_id] = (code, roll_at)
        if value is None or spot is None:
            continue
        bucket = days.setdefault(contract_id, {}).setdefault(ts.date(), [0, 0.0, 0.0, days_left])
        bucket[0] += 1
        bucket[1] += float(value)
        bucket[2] += float(spot)
        bucket[3] = min(bucket[3], days_left)
    found = []
    for contract_id, (code, roll_at) in meta.items():
        points = sorted(
            (left, total / count, price / count)
            for count, total, price, left in days.get(contract_id, {}).values()
            if count >= MIN_BARS_DAY
        )
        # Два дня с одним days_left (выходные внутри окна не дают сдвига) — берём последний.
        unique = {}
        for left, value, price in points:
            unique[left] = (left, value, price)
        letter, year = contract_month(code, roll_at)
        found.append({
            "id": contract_id, "code": code, "month": letter, "year": year,
            "live": roll_at is None or roll_at > now,
            "points": sorted(unique.values()),
        })
    found.sort(key=lambda item: (item["year"], MONTHS.index(item["month"]) if item["month"] in MONTHS else 0, item["code"]))
    return found
