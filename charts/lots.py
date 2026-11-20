"""Соотношение лотов ног спреда для сделки — вкладка «Лоты» на странице графика (/api/lots/).

Стоимость лота в рублях — по спецификации Мосбиржи (ISS): цена × STEPPRICE / MINSTEP.
STEPPRICE — стоимость шага цены в рублях, у долларовых контрактов (GD, SV, NG, BR)
в ней уже курс, поэтому лоты всех ног сравнимы. Цена — последняя 5-минутка ноги в базе,
нет её — расчётная цена биржи (PREVSETTLEPRICE).

Самая дорогая нога — 1 лот, остальные подбираются по стоимости и округляются.
Если ошибка округления больше TOLERANCE, ищется число лотов дорогой ноги до MAX_LOTS,
при котором она укладывается («точно»).

Какие ноги считаются:
- обычные ноги ФОРТС (символ @RTSX): квартал, вечный, ноги треугольников;
- CRCNH, SI6E, CRSISPOT, SIEUSPOT — только CR, Si, Eu: третья нога (CME, спот) не в счёт;
- NGNYMEX, NGNYMEX2, BRNYMEX — константой: лот NYMEX в 100 раз больше лота Мосбиржи
  (NG — 10 000 против 100 MMBtu, BZ — 1000 против 10 баррелей);
- спот Мосбиржи (@MISX) и ноги CME не считаются;
- календари и готовые спреды (NGCAL) — строки нет.
"""

import time

import httpx

ISS = "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
TOLERANCE = 0.03
MAX_LOTS = 10
CACHE_SECONDS = 3600
# NYMEX к Мосбирже: во сколько раз лот NYMEX больше лота Мосбиржи.
NYMEX_PER_MOEX = 100
_cache = {}


def specs(secids, fetch=None):
    """{secid: (minstep, stepprice, settle)} с Мосбиржи, кэш на CACHE_SECONDS."""
    now = time.monotonic()
    missing = [secid for secid in secids if secid not in _cache or now - _cache[secid][0] > CACHE_SECONDS]
    if missing:
        rows = (fetch or _fetch)(missing)
        for secid, row in rows.items():
            _cache[secid] = (now, row)
    return {secid: _cache[secid][1] for secid in secids if secid in _cache}


def _fetch(secids):
    response = httpx.get(ISS, params={
        "securities": ",".join(secids), "iss.only": "securities", "iss.meta": "off",
        "securities.columns": "SECID,MINSTEP,STEPPRICE,PREVSETTLEPRICE",
    }, timeout=5.0)
    response.raise_for_status()
    block = response.json().get("securities") or {}
    columns = block.get("columns") or []
    out = {}
    for row in block.get("data") or []:
        item = dict(zip(columns, row))
        if item.get("MINSTEP") and item.get("STEPPRICE"):
            out[item["SECID"]] = (float(item["MINSTEP"]), float(item["STEPPRICE"]), float(item.get("PREVSETTLEPRICE") or 0))
    return out


def ratio(costs):
    """costs [(имя, стоимость лота в ₽)] → (минимальный [(имя, лотов)], точный или None, ошибки)."""
    anchor = max(cost for _name, cost in costs)

    def lots(n):
        rows = [(name, max(1, round(n * anchor / cost))) for name, cost in costs]
        error = max(abs(count * cost - n * anchor) / (n * anchor) for (_name, count), (_n, cost) in zip(rows, costs))
        return rows, error

    minimal, error = lots(1)
    if error <= TOLERANCE:
        return minimal, None, (error, None)
    best = min((lots(n) + (n,) for n in range(2, MAX_LOTS + 1)), key=lambda item: (item[1] > TOLERANCE, item[2] if item[1] <= TOLERANCE else item[1]))
    return minimal, best[0], (error, best[1])


def _fmt(rows):
    return " : ".join(f"{name} {count}" for name, count in rows)


def table(pair, prices=None, fetch=None):
    """Лоты пары для вкладки «Лоты»: {"legs": [...], "minimal", "exact", ошибки, "note"}.

    legs — от дорогой ноги к дешёвой: secid, цена, MINSTEP, STEPPRICE, стоимость лота ₽,
    лотов в минимальном и точном варианте. note — почему считать нечего или как посчитано.
    prices — {secid: цена}, иначе последняя 5-минутка ноги в базе.
    """
    if pair.calendar or pair.listed:
        return {"legs": [], "note": "календарь: обе ноги одного актива, лоты 1 : 1"}
    if pair.ngnymex or pair.nymex:
        return {"legs": [], "note": f"Мосбиржа {NYMEX_PER_MOEX} : NYMEX 1 — лот NYMEX в {NYMEX_PER_MOEX} раз больше лота Мосбиржи"}
    from market.live import pair_symbols
    from datetime import datetime, timezone
    symbols = [symbol for symbol in dict.fromkeys(pair_symbols(pair, datetime.now(timezone.utc), full=True)) if symbol]
    # Только ФОРТС: CME (CNH, 6E) и спот Мосбиржи — не в счёт; у кросс-пар остаются CR, Si, Eu.
    secids = [symbol.split("@")[0] for symbol in symbols if symbol.endswith("@RTSX")]
    skipped = [symbol.split("@")[0] for symbol in symbols if not symbol.endswith("@RTSX")]
    if len(secids) < 2:
        return {"legs": [], "note": "меньше двух ног на ФОРТС — считать нечего"}
    found = specs(secids, fetch)
    prices = prices if prices is not None else _prices(symbols)
    legs = []
    for secid in secids:
        if secid not in found:
            return {"legs": [], "note": f"Мосбиржа не отдала спецификацию {secid}"}
        minstep, stepprice, settle = found[secid]
        price = prices.get(secid) or settle
        if not price:
            return {"legs": [], "note": f"нет цены {secid}"}
        legs.append({"secid": secid, "price": price, "minstep": minstep, "stepprice": stepprice,
                     "cost": price * stepprice / minstep, "live": bool(prices.get(secid))})
    legs.sort(key=lambda leg: -leg["cost"])  # от дорогой ноги к дешёвой
    minimal, exact, (error, exact_error) = ratio([(leg["secid"], leg["cost"]) for leg in legs])
    for leg, (_name, count) in zip(legs, minimal):
        leg["minimal"] = count
    for leg, (_name, count) in zip(legs, exact or []):
        leg["exact"] = count
    note = "не в счёт: " + ", ".join(skipped) if skipped else ""
    return {"legs": legs, "minimal_error": error, "exact_error": exact_error, "note": note}


def line(pair, prices=None, fetch=None):
    """Одной строкой «Лоты: …» или пусто — для логов и проверок."""
    found = table(pair, prices, fetch)
    legs = found["legs"]
    if not legs:
        return "Лоты: " + found["note"] if "NYMEX" in found.get("note", "") else ""
    anchor = legs[0]
    text = (f"Лоты: {_fmt([(leg['secid'], leg['minimal']) for leg in legs])} "
            f"(1 лот {anchor['secid']} ≈ {anchor['cost']:,.0f} ₽").replace(",", " ")
    text += f", ошибка {found['minimal_error'] * 100:.1f}%)"
    if found["exact_error"] is not None:
        text += f"; точнее: {_fmt([(leg['secid'], leg['exact']) for leg in legs])} (ошибка {found['exact_error'] * 100:.1f}%)"
    return text


def _prices(symbols):
    """Последняя 5-минутка каждой ноги ФОРТС: {secid: цена}."""
    from market.models import Instrument, MinuteCandle
    out = {}
    for item in Instrument.objects.filter(finam_symbol__in=symbols):
        close = MinuteCandle.objects.filter(instrument=item).order_by("-ts").values_list("close", flat=True).first()
        if close:
            out[item.finam_symbol.split("@")[0]] = float(close)
    return out
