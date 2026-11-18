"""Торговые сессии по типам инструментов, UTC.

По ним стыкуются ноги спреда на мелких таймфреймах и размечается шкала
графика. Сами бары берутся из факта (есть свеча — торговали), сессии нужны,
чтобы подставлять последнюю цену редкой ноги только в её же торговое время
и не писать бары вне торгов.

ФОРТС (@RTSX)   будни 04:00–20:50, выходные 07:00–16:00 (сессии выходного дня)
споты металлов  будни 06:00–16:00 (GLDRUB_TOM, SLVRUB_TOM)
споты валют     будни 04:00–20:50 (USD000UTSTOM, CNYRUB_TOM) — уточнить по данным
CME, NYMEX      вс 22:00 — пт 21:00, ежедневный перерыв 21:00–22:00
Bybit           круглосуточно
Праздники Мосбиржи отдельно не размечены: в праздник у ноги просто нет свечей.
"""

from datetime import time, timedelta, timezone

FORTS = "forts"
METAL_SPOT = "metal_spot"
FX_SPOT = "fx_spot"
CME = "cme"
ALWAYS = "always"

# (будни, выходные): (начало, конец) — конец не включается; None — не торгуется.
HOURS = {
    FORTS: ((time(4, 0), time(20, 50)), (time(7, 0), time(16, 0))),
    METAL_SPOT: ((time(6, 0), time(16, 0)), None),
    FX_SPOT: ((time(4, 0), time(20, 50)), None),
}
METAL_PREFIXES = ("GLD", "SLV", "PLT", "PLD")


def kind_of(symbol="", venue=""):
    """Тип сессии по символу Finam (CRZ6@RTSX, GLDRUB_TOM@MISX, CNHZ26@XCME) или площадке."""
    if venue == "bybit":
        return ALWAYS
    symbol = (symbol or "").upper()
    if symbol.endswith(("@XCME", "@XNYM", "@XCBT", "@XCEC")):
        return CME
    if symbol.endswith("@MISX"):
        return METAL_SPOT if symbol.startswith(METAL_PREFIXES) else FX_SPOT
    return FORTS


def instrument_kind(instrument):
    pair = getattr(instrument, "pair", None)
    return kind_of(getattr(instrument, "finam_symbol", ""), getattr(pair, "venue", ""))


def _utc(ts):
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def in_session(kind, ts):
    """Торгуется ли инструмент этого типа в момент ts."""
    ts = _utc(ts)
    if kind == ALWAYS:
        return True
    if kind == CME:
        day, clock = ts.weekday(), ts.time()
        if day == 5 or (day == 6 and clock < time(22, 0)) or (day == 4 and clock >= time(21, 0)):
            return False
        return not (time(21, 0) <= clock < time(22, 0))
    weekday, weekend = HOURS.get(kind, HOURS[FORTS])
    window = weekday if ts.weekday() < 5 else weekend
    if window is None:
        return False
    start, end = window
    return start <= ts.time() < end


def bar_in_session(kind, ts, step_minutes):
    """Задевает ли бар [ts, ts + шаг) сессию: часовой бар 20:00 ещё торгуется до 20:50."""
    ts = _utc(ts)
    probe, end = ts, ts + timedelta(minutes=step_minutes)
    while probe < end:
        if in_session(kind, probe):
            return True
        probe += timedelta(minutes=5)
    return False


def forts_hour(ts):
    """Часовой бар внутри сессии ФОРТС, включая выходные: будни 04–20, выходные 07–15 UTC."""
    return bar_in_session(FORTS, _utc(ts).replace(minute=0, second=0, microsecond=0), 60)
