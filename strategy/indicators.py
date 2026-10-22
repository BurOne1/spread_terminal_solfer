"""spread_rate = raw − спот · (RUONIA − r_foreign) · t / 365.

Ставки в процентах годовых, поэтому деление на 100.
У золота r_foreign = 0: raw − спот · RUONIA / 100 · t / 365.
"""


def days_left(value: int) -> int:
    return max(int(value), 1)


def fair_spread(spot: float, ruonia: float, foreign_rate: float, days: int) -> float:
    return spot * (ruonia - foreign_rate) / 100.0 * (days_left(days) / 365.0)


def spread_rate(raw: float, spot: float, ruonia: float, foreign_rate: float, days: int) -> float:
    return raw - fair_spread(spot, ruonia, foreign_rate, days)


def ewma(values, span=14):
    alpha = 2 / (span + 1)
    acc = float(values[0])
    for value in values[1:]:
        acc = alpha * float(value) + (1 - alpha) * acc
    return acc


def ewma_series(rows, span=14):
    """rows: (ts, 8ч ставка). Возвращает (ts, ewma) в тех же долях."""
    alpha = 2 / (span + 1)
    out = []
    acc = None
    for ts, rate in rows:
        acc = float(rate) if acc is None else alpha * float(rate) + (1 - alpha) * acc
        out.append((ts, acc))
    return out


def fair_from_funding(spot, ewma_8h, days):
    """8ч ставка × 3 интервала в день даёт дневную, дальше как годовая × t/365."""
    return float(spot) * float(ewma_8h) * 3 * days_left(days)