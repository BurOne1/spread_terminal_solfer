"""Пересчёт strategy_spreadbar из свечей и ставок, которые уже в базе.

Считает выбранную пару или все активные. Окно квартала — listed_at до roll_at.
С since пересчитывается только хвост: бары не раньше since. Свечи читаются
с запасом LOOKBACK назад, чтобы спот и окно контракта были известны.

history_bars считает тот же ряд для аналитики, но окно каждого контракта
начинается за HISTORY_MONTHLY или HISTORY_QUARTERLY до склейки: окна
перекрываются, бары не пишутся в базу.

Живые потоки не пересчитывают на каждый тик: они зовут mark_dirty,
а flush_dirty раз в несколько секунд пересчитывает хвосты отмеченных пар.

Календарь (foreign_source CAL) — секонд минус фронт кварталов базы.
Треугольник с base — тот же треугольник, но все квартальные ноги вторые.
Окно и склейка у обоих — фронта: в третий четверг после склейки SiZ2026
пара переходит с SiZ2026/SiH2027 на SiH2027/SiM2027.
"""

import threading
from datetime import timedelta, timezone

from django.db import transaction

from market.models import Candle, Instrument, Pair
from rates.models import RateDay
from strategy.build import build_points, money, stepper
from strategy.models import SpreadBar

LOOKBACK = timedelta(days=7)
RATE_LOOKBACK = timedelta(days=30)

# История для аналитики: два месяца у месячных контрактов, два квартала у квартальных.
HISTORY_MONTHLY = timedelta(days=61)
HISTORY_QUARTERLY = timedelta(days=183)

_lock = threading.Lock()
_dirty = {}
_dirty_lock = threading.Lock()
# Режим истории: span — насколько раньше склейки открыть окно, rows — куда собрать бары.
_history = threading.local()


def history_span(pair):
    """Глубина истории контракта пары: месячные — нефть и газ, остальные квартальные."""
    return HISTORY_MONTHLY if (pair.nymex or pair.listed or pair.ngnymex) else HISTORY_QUARTERLY


def history_bars(pair, span=None):
    """Бары пары с окнами от склейки − span до склейки, без записи в базу.

    Окна соседних контрактов перекрываются: у одного часа может быть
    несколько баров, по одному на контракт.
    """
    _history.span = span or history_span(pair)
    _history.rows = []
    try:
        _rebuild_one(pair, None)
        return _history.rows
    finally:
        _history.span = None
        _history.rows = None


def _span():
    return getattr(_history, "span", None)


def rebuild_spread(pair_code="", since=None):
    pairs = Pair.objects.filter(active=True)
    if pair_code:
        pairs = pairs.filter(code=pair_code)
    return sum(_rebuild_one(pair, since) for pair in pairs)


def _rebuild_one(pair, since):
    if pair.foreign_source == "TRI":
        return _rebuild_tri(pair, since)
    if pair.calendar:
        return _rebuild_calendar(pair, since)
    if pair.nymex:
        return _rebuild_nymex(pair, since)
    if pair.cross:
        return _rebuild_cross(pair, since)
    if pair.listed:
        return _rebuild_listed(pair, since)
    if pair.ngnymex:
        return _rebuild_ngnymex(pair, since)
    return _rebuild_pair(pair, since)


# Треугольник собирается из чужих ног: свечи этих пар меняют и его.
TRI_DEPS = {
    "TRIGD": ("TRIGD", "GOLD", "USD"),
    "TRISV": ("TRISV", "SILVER", "USD"),
    "TRIGL": ("TRIGD", "GOLD", "USD"),
}
# Треугольник на споте золота — только если такая пара заведена (add_extra_spreads).
TRI_SPOT_DEPS = {"TRIGDS": ("TRIGD", "GOLD", "USD")}


def mark_dirty(pair_code, since):
    """Запомнить, что у пары поменялись свечи начиная с since."""
    if not pair_code:
        return
    with _dirty_lock:
        old = _dirty.get(pair_code)
        _dirty[pair_code] = since if old is None or since < old else old


def _dependencies():
    """Пара → пары, чьи свечи её меняют. Календари и секонды — из базы."""
    from market.cross import CROSS
    deps = dict(TRI_DEPS)
    # Кросс-пары берут кварталы Мосбиржи у USD, CNY, EUR.
    crosses = Pair.objects.filter(active=True, foreign_source="CMEFX", code__in=list(CROSS)).values_list("code", flat=True)
    deps.update({code: (code, *CROSS[code]["deps"]) for code in crosses})
    spot = Pair.objects.filter(active=True, code__in=list(TRI_SPOT_DEPS)).values_list("code", flat=True)
    deps.update({code: TRI_SPOT_DEPS[code] for code in spot})
    rows = Pair.objects.filter(active=True).exclude(base="").values_list("code", "base", "foreign_source")
    for code, base, source in rows:
        deps[code] = TRI_DEPS.get(base, (base,)) if source == "TRI" else (base,)
    return deps


def flush_dirty():
    """Пересчитать хвосты отмеченных пар и всех, кто из них собран. Вернуть {код: баров}."""
    with _dirty_lock:
        batch = dict(_dirty)
        _dirty.clear()
    if not batch:
        return {}
    deps = _dependencies()
    for code, since in list(batch.items()):
        for follower, needs in deps.items():
            if code in needs:
                batch[follower] = min(batch.get(follower, since), since)
    done = {}
    for code, since in sorted(batch.items()):
        try:
            done[code] = rebuild_spread(code, since=since)
        except Exception:
            # Непересчитанные пары остаются отмеченными до следующего раза.
            for left, left_since in batch.items():
                if left not in done:
                    mark_dirty(left, left_since)
            raise
    return done


def _load_from(since):
    return None if since is None else since - LOOKBACK


def _ohlc(instrument, load_from):
    """Час → (open, close). Наличие бара — час этой ноги."""
    if instrument is None:
        return {}
    rows = Candle.objects.filter(instrument=instrument)
    if load_from is not None:
        rows = rows.filter(ts__gte=load_from)
    return {ts: (open_, close) for ts, open_, close in rows.values_list("ts", "open", "close")}


def _ohlc_many(instruments, load_from):
    """{id инструмента: {час: (open, close)}} одним запросом."""
    found = {instrument.id: {} for instrument in instruments}
    if not found:
        return found
    rows = Candle.objects.filter(instrument_id__in=list(found))
    if load_from is not None:
        rows = rows.filter(ts__gte=load_from)
    for instrument_id, ts, open_, close in rows.values_list("instrument_id", "ts", "open", "close"):
        found[instrument_id][ts] = (open_, close)
    return found


def _quarters(pair, load_from, scale=1):
    """Кварталы пары по порядку склейки, бары (open, close) / scale.

    Кварталы без баров тоже в списке: по соседству ищется секонд.
    С хвостом пропускаются только склеенные до load_from, порядок не рвётся.
    """
    if pair is None:
        return []
    instruments = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__isnull=False)
    if load_from is not None:
        instruments = instruments.filter(roll_at__gt=load_from)
    instruments = list(instruments.order_by("roll_at"))
    bars = _ohlc_many(instruments, load_from)
    span = _span()
    return [
        {
            "id": instrument.id,
            "code": instrument.code,
            "listed_at": instrument.listed_at if span is None else min(
                instrument.roll_at - span, instrument.listed_at or instrument.roll_at,
            ),
            "roll_at": instrument.roll_at,
            "expires": instrument.roll_at,
            "bars": bars[instrument.id] if scale == 1 else {
                ts: (open_ / scale, close / scale) for ts, (open_, close) in bars[instrument.id].items()
            },
        }
        for instrument in instruments
    ]


def _seconds(quarters):
    """Вторые контракты в окнах фронта.

    Окно, id и код — фронта, цены и экспирация — следующего квартала.
    front — бары самого фронта, они нужны календарю.
    """
    return [
        {
            **second,
            "id": front["id"],
            "code": front["code"],
            "listed_at": front["listed_at"],
            "roll_at": front["roll_at"],
            "front": front["bars"],
        }
        for front, second in zip(quarters, quarters[1:])
    ]


def _traded(quarters):
    return [row for row in quarters if row["bars"]]


def _stepper(rows):
    """Последнее непустое значение на ключ или раньше, во float. rows по порядку ключа."""
    return stepper([(key, float(value)) for key, value in rows if value is not None])


def _rate_rows(load_from):
    """Ставки с запасом назад: в праздники берётся последняя известная."""
    rows = RateDay.objects.order_by("date")
    if load_from is not None:
        rows = rows.filter(date__gte=load_from.date() - RATE_LOOKBACK)
    return list(rows)


def _side(rows, index, scale=1):
    return {ts: prices[index] / scale for ts, prices in rows.items()}


def _rebuild_pair(pair, since=None):
    scale = pair.future_scale or 1
    load_from = _load_from(since)
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None:
        return 0
    perp_rows = _ohlc(perp, load_from)
    if not perp_rows:
        return 0
    spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
    spot_rows = _ohlc(spot, load_from) if spot is not None else {}
    futures = _traded(_quarters(pair, load_from, scale))
    if not futures:
        return 0
    funding = None
    rates = []
    if pair.foreign_source == "EWMA14":
        from rates.models import FundingPoint
        funding = list(
            FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
        )
    else:
        rates = _pair_rates(pair.foreign_source, _rate_rows(load_from))

    def points_at(index):
        if _span() is not None:
            # Окна перекрываются: каждый контракт отдельно, ключ — (час, контракт).
            found = {}
            for contract in contracts_at(index):
                for item in build_points(
                    _side(perp_rows, index), [contract], rates, _side(spot_rows, index), funding=funding,
                ):
                    found[(item["ts"], item["contract_id"])] = item
            return found
        return {(item["ts"], item["contract_id"]): item for item in build_points(
            _side(perp_rows, index), contracts_at(index), rates, _side(spot_rows, index), funding=funding, since=since,
        )}

    def contracts_at(index):
        return [
            {
                "id": row["id"],
                "code": row["code"],
                "listed_at": row["listed_at"],
                "roll_at": row["roll_at"],
                "candles": _side(row["bars"], index),
            }
            for row in futures
        ]

    closes = points_at(1)
    opens = points_at(0)
    rows = []
    for key in sorted(closes):
        ts = key[0]
        if since is not None and ts < since:
            continue
        item = closes[key]
        opened = opens.get(key) or {}
        rows.append(SpreadBar(
            pair=pair,
            ts=ts,
            contract_id=item["contract_id"],
            future_close=item["future_close"],
            perp_close=item["perp_close"],
            spot=item["spot"],
            raw_spread=money(item["raw_spread"]),
            days_left=item["days_left"],
            ruonia=money(item["ruonia"]),
            foreign_rate=money(item["foreign_rate"]),
            fair=money(item["fair"]),
            spread_rate=money(item["spread_rate"]),
            raw_open=money(opened.get("raw_spread")),
            rate_open=money(opened.get("spread_rate")),
        ))
    return _store(pair, rows, since)


def _pair_rates(source, rate_days):
    """(дата, рублёвая ставка, иностранная) для fair = спот · (r − r_foreign) · t / 365."""
    if source == "NONE":
        return [(row.date, row.ruonia, 0) for row in rate_days]
    field = {"DFEDTARU": "dfedtaru", "ESTR": "estr"}.get(source, "shibor")
    return [(row.date, row.ruonia, getattr(row, field)) for row in rate_days]


def _rebuild_calendar(pair, since=None):
    """Секонд минус фронт кварталов базы, в единицах базы.

    fair = спот · (r − r_foreign) / 100 · (t секонда − t фронта) / 365:
    разница двух форвардов от одного спота. Спот — спот базы, его нет — фронт.
    У долларовых GD и SV рублёвой ноги нет: r — ставка ФРС, r_foreign = 0,
    спот — фронт, спред в долларах. В поле ruonia бара лежит r.
    days_left — дни до склейки фронта, когда пара перейдёт на следующие кварталы.
    Час без бара одной из ног пропускается, как у остальных пар.
    """
    base = Pair.objects.filter(code=pair.base).first()
    if base is None:
        return 0
    load_from = _load_from(since)
    windows = [
        row for row in _seconds(_quarters(base, load_from, base.future_scale or 1))
        if row["front"] and row["bars"]
    ]
    if not windows:
        return 0
    rate_days = _rate_rows(load_from)
    spot_rows = {}
    if base.foreign_source == "TRI":
        domestic = _stepper([(row.date, row.dfedtaru) for row in rate_days])
        foreign = _stepper([(row.date, 0) for row in rate_days])
    else:
        rates = _pair_rates(base.foreign_source, rate_days)
        domestic = _stepper([(day, value) for day, value, _other in rates])
        foreign = _stepper([(day, value) for day, _value, value in rates])
        spot = Instrument.objects.filter(pair=base, code=base.spot_code).first()
        spot_rows = _ohlc(spot, load_from)
    spots = [_stepper(sorted(_side(spot_rows, index).items())) for index in (0, 1)]
    rows = []
    for window in windows:
        roll, expires = window["roll_at"], window["expires"]
        tenor = max((expires.date() - roll.date()).days, 1)
        for ts in sorted(window["front"]):
            if since is not None and ts < since:
                continue
            if window["listed_at"] is not None and ts < window["listed_at"]:
                continue
            if ts >= roll:
                continue
            far = window["bars"].get(ts)
            if far is None:
                continue
            near = window["front"][ts]
            day = ts.astimezone(timezone.utc).date()
            r, r_foreign = domestic(day), foreign(day)
            values = []
            for index in (0, 1):
                raw = float(far[index]) - float(near[index])
                spot = spots[index](ts) or float(near[index])
                fair = None if r is None or r_foreign is None else spot * (r - r_foreign) / 100.0 * tenor / 365.0
                values.append((raw, spot, fair, None if fair is None else raw - fair))
            (raw_open, _spot_open, _fair_open, rate_open), (raw, spot, fair, rate) = values
            rows.append(SpreadBar(
                pair=pair,
                ts=ts,
                contract_id=window["id"],
                future_close=money(float(far[1])),
                perp_close=money(float(near[1])),
                spot=money(spot),
                raw_spread=money(raw),
                days_left=_days(roll, ts)[0],
                ruonia=money(r),
                foreign_rate=money(r_foreign),
                fair=money(fair),
                spread_rate=money(rate),
                raw_open=money(raw_open),
                rate_open=money(rate_open),
            ))
    return _store(pair, rows, since)


def _rebuild_nymex(pair, since=None):
    """BR Мосбиржи минус BZ NYMEX того же окна, доллары за баррель.

    Обе ноги рассчитываются по одной цене ICE Brent: fair = 0, spread_rate = raw.
    Бар есть, только если в этот час торговали обе биржи и это часы
    Мосбиржи в будни: выходные сессии, праздники Лондона и Нью-Йорка,
    ночь NYMEX и перерывы пропускаются сами. spot — цена BZ, от неё уровни.
    days_left — дни до экспирации BZ, тогда пара переходит на следующий месяц.
    """
    load_from = _load_from(since)
    windows = _traded(_quarters(pair, load_from))
    if not windows:
        return 0
    legs = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FOREIGN, roll_at__isnull=False)
    if load_from is not None:
        legs = legs.filter(roll_at__gt=load_from)
    legs = list(legs)
    bars = _ohlc_many(legs, load_from)
    foreign = {leg.roll_at: bars[leg.id] for leg in legs}
    rows = []
    for window in windows:
        roll = window["roll_at"]
        far = foreign.get(roll)
        if not far:
            continue
        for ts in sorted(window["bars"]):
            if since is not None and ts < since:
                continue
            if window["listed_at"] is not None and ts < window["listed_at"]:
                continue
            if ts >= roll or not _common_hour(ts):
                continue
            nymex = far.get(ts)
            if nymex is None:
                continue
            moex = window["bars"][ts]
            raw_open = float(moex[0]) - float(nymex[0])
            raw = float(moex[1]) - float(nymex[1])
            rows.append(SpreadBar(
                pair=pair,
                ts=ts,
                contract_id=window["id"],
                future_close=money(float(moex[1])),
                perp_close=money(float(nymex[1])),
                spot=money(float(nymex[1])),
                raw_spread=money(raw),
                days_left=_days(roll, ts)[0],
                ruonia=None,
                foreign_rate=None,
                fair=money(0.0),
                spread_rate=money(raw),
                raw_open=money(raw_open),
                rate_open=money(raw_open),
            ))
    return _store(pair, rows, since)


def _rebuild_cross(pair, since=None):
    """a × b × mult − c за час, где торговали все три ноги, в будний час Мосбиржи.

    Окна — кварталы CME пары (см. market/cross.py), ноги Мосбиржи — кварталы
    того же месяца у пар USD, CNY, EUR. fair = 0, spread_rate = raw; spot —
    цена c (Si или Eu), от неё уровни. days_left — до экспирации CME.
    """
    from market.cross import CROSS, legs, parse_code

    spec = CROSS.get(pair.code)
    if spec is None:
        return 0
    load_from = _load_from(since)
    windows = _traded(_quarters(pair, load_from))
    if not windows:
        return 0
    wanted = {}
    for window in windows:
        a, _b, c = legs(spec, *parse_code(spec, window["code"]))
        wanted[window["id"]] = (a, c)
    symbols = {symbol for pairs in wanted.values() for symbol in pairs}
    instruments = list(Instrument.objects.filter(finam_symbol__in=symbols))
    bars = _ohlc_many(instruments, load_from)
    by_symbol = {instrument.finam_symbol: bars[instrument.id] for instrument in instruments}
    mult = spec["mult"]
    rows = []
    for window in windows:
        a_symbol, c_symbol = wanted[window["id"]]
        a_bars, c_bars = by_symbol.get(a_symbol), by_symbol.get(c_symbol)
        if not a_bars or not c_bars:
            continue
        roll = window["roll_at"]
        for ts in sorted(window["bars"]):
            if since is not None and ts < since:
                continue
            if window["listed_at"] is not None and ts < window["listed_at"]:
                continue
            if ts >= roll or not _common_hour(ts):
                continue
            a, c = a_bars.get(ts), c_bars.get(ts)
            if a is None or c is None:
                continue
            b = window["bars"][ts]
            synthetic = [float(a[index]) * float(b[index]) * mult for index in (0, 1)]
            raw_open, raw = synthetic[0] - float(c[0]), synthetic[1] - float(c[1])
            rows.append(SpreadBar(
                pair=pair, ts=ts, contract_id=window["id"],
                future_close=money(synthetic[1]), perp_close=money(float(c[1])), spot=money(float(c[1])),
                raw_spread=money(raw), days_left=_days(roll, ts)[0], ruonia=None, foreign_rate=money(float(b[1])),
                fair=money(0.0), spread_rate=money(raw), raw_open=money(raw_open), rate_open=money(raw_open),
            ))
    return _store(pair, rows, since)


def _rebuild_ngnymex(pair, since=None):
    """NG Мосбиржи минус NG NYMEX месяцем позже, $/MMBtu, только общие будние часы Мосбиржи.

    Окна — ноги NYMEX пары (market/ngnymex.py), ноги Мосбиржи — по символу.
    fair = 0, spread_rate = raw, spot — цена NYMEX, от неё уровни.
    """
    from market.ngnymex import moex_code

    load_from = _load_from(since)
    windows = _traded(_quarters(pair, load_from))
    if not windows:
        return 0
    # Нога Мосбиржи — по коду (NGZ2026): символ NGZ6@RTSX повторяется раз в десять лет.
    moex_of = {window["id"]: moex_code(window["code"]) for window in windows}
    instruments = list(Instrument.objects.filter(code__in=set(moex_of.values())))
    bars = _ohlc_many(instruments, load_from)
    by_symbol = {instrument.code: bars[instrument.id] for instrument in instruments}
    rows = []
    for window in windows:
        moex_bars = by_symbol.get(moex_of[window["id"]])
        if not moex_bars:
            continue
        roll = window["roll_at"]
        for ts in sorted(window["bars"]):
            if since is not None and ts < since:
                continue
            if window["listed_at"] is not None and ts < window["listed_at"]:
                continue
            if ts >= roll or not _common_hour(ts):
                continue
            moex = moex_bars.get(ts)
            if moex is None:
                continue
            nymex = window["bars"][ts]
            raw_open, raw = float(moex[0]) - float(nymex[0]), float(moex[1]) - float(nymex[1])
            rows.append(SpreadBar(
                pair=pair, ts=ts, contract_id=window["id"],
                future_close=money(float(moex[1])), perp_close=money(float(nymex[1])), spot=money(float(nymex[1])),
                raw_spread=money(raw), days_left=_days(roll, ts)[0], ruonia=None, foreign_rate=None,
                fair=money(0.0), spread_rate=money(raw), raw_open=money(raw_open), rate_open=money(raw_open),
            ))
    return _store(pair, rows, since)


def _rebuild_listed(pair, since=None):
    """Готовый спред биржи (NGX6NGZ6@RTSX): raw — его цена, fair = 0.

    Ноги не нужны: Мосбиржа котирует календарь одним инструментом. Уровни
    у такой пары абсолютные, в единицах спреда ($/MMBtu), поэтому spot = 1:
    движок умножает уровень на spot (strategy/engine.py).
    """
    load_from = _load_from(since)
    windows = _traded(_quarters(pair, load_from))
    if not windows:
        return 0
    rows = []
    for window in windows:
        roll = window["roll_at"]
        for ts in sorted(window["bars"]):
            if since is not None and ts < since:
                continue
            if window["listed_at"] is not None and ts < window["listed_at"]:
                continue
            if ts >= roll:
                continue
            opened, closed = (float(value) for value in window["bars"][ts])
            rows.append(SpreadBar(
                pair=pair, ts=ts, contract_id=window["id"],
                future_close=money(closed), perp_close=money(closed), spot=money(1.0),
                raw_spread=money(closed), days_left=_days(roll, ts)[0], ruonia=None, foreign_rate=None,
                fair=money(0.0), spread_rate=money(closed), raw_open=money(opened), rate_open=money(opened),
            ))
    return _store(pair, rows, since)


OZ_GRAMS = 31.1034768
# Чей вечный в треугольнике. Ключ — pair.tri_code: у секондов это база.
TRI_PERP = {"TRIGD": "GOLD", "TRISV": "SILVER", "TRIGL": "GOLD", "TRIGDS": "GOLD"}
# Треугольники со спотом металла вместо вечного: фандинга нет.
TRI_SPOT = {"TRIGDS"}
# Чьи кварталы металла берёт треугольник, если не свои.
TRI_OWNER = {"TRIGDS": "TRIGD"}


def _common_hour(ts):
    """Пересечение сессий FORTS: будни, 07:00–23:50 МСК = 04:00–20:00 UTC."""
    ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if ts.weekday() >= 5:
        return False
    return 4 <= ts.hour <= 20


def _window_price(contracts, ts, roll=None):
    """Цена ноги в час ts: контракт, в окне которого этот час.

    В режиме истории окна перекрываются — берётся нога с той же склейкой (roll).
    """
    if roll is not None and _span() is not None:
        for contract in contracts:
            if contract["roll_at"] == roll and ts < roll and (contract["listed_at"] is None or ts >= contract["listed_at"]):
                price = contract["bars"].get(ts)
                if price is not None:
                    return price, contract
    for contract in contracts:
        left = contract["listed_at"]
        if left is not None and ts < left:
            continue
        if ts >= contract["roll_at"]:
            continue
        price = contract["bars"].get(ts)
        if price is not None:
            return price, contract
    return None, None


def _days(roll, ts):
    day = ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
    return max((roll.date() - day).days, 1), day


def _tri_rates(load_from):
    """RUONIA и ставка ФРС на дату, последние известные."""
    rate_days = _rate_rows(load_from)
    return (
        _stepper([(row.date, row.ruonia) for row in rate_days]),
        _stepper([(row.date, row.dfedtaru) for row in rate_days]),
    )


def _si_contracts(usd, load_from):
    scale = usd.future_scale if usd.future_scale and usd.future_scale > 1 else 1000
    return _quarters(usd, load_from, scale)


def _legs(pair, *quarters):
    """Квартальные ноги треугольника: свои или, у пары с base, вторые."""
    if pair.base:
        quarters = [_seconds(rows) for rows in quarters]
    return [_traded(rows) for rows in quarters]


def _tri_values(metal_usd, dollar, perp_rub, ruonia, fed, days):
    """raw, fair и очищенный спред треугольника в рублях за грамм."""
    perp_gram = float(perp_rub)
    raw = (float(metal_usd) * float(dollar) - perp_gram * OZ_GRAMS) / OZ_GRAMS
    # Ставку ФРС можно убрать: у синтетики металл_USD × SI долларовая
    # ставка сокращается, и кэрри остаётся ≈ RUONIA. Оставлена намеренно.
    fair = perp_gram * ((ruonia or 0) + (fed or 0)) / 100.0 * days / 365.0
    return raw, fair, raw - fair


def _tri_row(pair, ts, contract_id, roll, rates, metal_usd, dollar, perp_rub, window_roll=None):
    """SpreadBar треугольника. Цены ног — пары (open, close).

    roll — ближайшая экспирация ног, по ней срок в fair. window_roll —
    склейка окна для days_left. У секондов это склейка фронта, она раньше.
    """
    days = _days(roll, ts)[0]
    day = ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
    ruonia, fed = rates[0](day), rates[1](day)
    raw, fair, cleaned = _tri_values(metal_usd[1], dollar[1], perp_rub[1], ruonia, fed, days)
    raw_open, _fair_open, cleaned_open = _tri_values(metal_usd[0], dollar[0], perp_rub[0], ruonia, fed, days)
    return SpreadBar(
        pair=pair,
        ts=ts,
        contract_id=contract_id,
        future_close=money(float(metal_usd[1]) * float(dollar[1]) / OZ_GRAMS),
        perp_close=money(float(perp_rub[1])),
        spot=money(float(perp_rub[1])),
        raw_spread=money(raw),
        days_left=_days(window_roll or roll, ts)[0],
        ruonia=None if ruonia is None else money(ruonia),
        foreign_rate=money(dollar[1]),
        fair=money(fair),
        spread_rate=money(cleaned),
        raw_open=money(raw_open),
        rate_open=money(cleaned_open),
    )


def _rebuild_tri(pair, since=None):
    """GD+SI−GLDRUBF, SV+SI−SLVRUBF или GD+SI−GL. С base — на вторых кварталах.

    SI — на (RUONIA − ставка ФРС) и свой срок, металл — на ставку ФРС,
    рублёвая нога — на RUONIA. raw остаётся грязным. Склейка — самая ранняя из ног.
    foreign_rate у треугольника хранит курс SI, а не ставку.
    """
    if pair.tri_code == "TRIGL":
        return _rebuild_trigl(pair, since)
    load_from = _load_from(since)
    metal = Pair.objects.filter(code=TRI_PERP.get(pair.tri_code, "")).first()
    usd = Pair.objects.filter(code="USD").first()
    owner = Pair.objects.filter(code=TRI_OWNER.get(pair.tri_code, pair.tri_code)).first()
    if metal is None or usd is None or owner is None:
        return 0
    third = metal.spot_code if pair.tri_code in TRI_SPOT else metal.perp_code
    perp = Instrument.objects.filter(pair=metal, code=third).first()
    perp_bars = _ohlc(perp, load_from)
    si_contracts, metal_contracts = _legs(pair, _si_contracts(usd, load_from), _quarters(owner, load_from))
    if not perp_bars or not si_contracts or not metal_contracts:
        return 0
    rates = _tri_rates(load_from)
    rows = []
    for contract in metal_contracts:
        for ts in sorted(contract["bars"]):
            if since is not None and ts < since:
                continue
            if not _common_hour(ts):
                continue
            left = contract["listed_at"]
            if left is not None and ts < left:
                continue
            if ts >= contract["roll_at"]:
                continue
            metal_usd = contract["bars"].get(ts)
            perp_rub = perp_bars.get(ts)
            dollar, si = _window_price(si_contracts, ts, contract["roll_at"])
            if metal_usd is None or perp_rub is None or dollar is None:
                continue
            roll = contract["expires"]
            if si is not None and si["expires"] < roll:
                roll = si["expires"]
            rows.append(_tri_row(
                pair, ts, contract["id"], roll, rates, metal_usd, dollar, perp_rub, contract["roll_at"],
            ))
    return _store(pair, rows, since)


def _rebuild_trigl(pair, since=None):
    """GD+SI−GL. Все три ноги квартальные и уже в базе. С base — все вторые."""
    load_from = _load_from(since)
    gold = Pair.objects.filter(code="GOLD").first()
    gd = Pair.objects.filter(code="TRIGD").first()
    usd = Pair.objects.filter(code="USD").first()
    if gold is None or gd is None or usd is None:
        return 0
    gl_contracts, gd_contracts, si_contracts = _legs(
        pair, _quarters(gold, load_from), _quarters(gd, load_from), _si_contracts(usd, load_from),
    )
    if not gl_contracts or not gd_contracts or not si_contracts:
        return 0
    rates = _tri_rates(load_from)
    rows = []
    for gl in gl_contracts:
        for ts in sorted(gl["bars"]):
            if since is not None and ts < since:
                continue
            if not _common_hour(ts):
                continue
            left = gl["listed_at"]
            if left is not None and ts < left:
                continue
            if ts >= gl["roll_at"]:
                continue
            gl_rub = gl["bars"].get(ts)
            gd_usd, gd_contract = _window_price(gd_contracts, ts, gl["roll_at"])
            dollar, si = _window_price(si_contracts, ts, gl["roll_at"])
            if gl_rub is None or gd_usd is None or dollar is None:
                continue
            roll = gl["expires"]
            if gd_contract is not None and gd_contract["expires"] < roll:
                roll = gd_contract["expires"]
            if si is not None and si["expires"] < roll:
                roll = si["expires"]
            rows.append(_tri_row(pair, ts, gl["id"], roll, rates, gd_usd, dollar, gl_rub, gl["roll_at"]))
    return _store(pair, rows, since)


def _store(pair, rows, since=None):
    """Без since — заменить ряд пары целиком, с since — только хвост."""
    if _span() is not None:
        _history.rows.extend(rows)
        return len(rows)
    if not rows and since is None:
        return 0
    by_ts = {}
    for row in rows:
        by_ts[row.ts] = row
    rows = [by_ts[ts] for ts in sorted(by_ts)]
    with _lock, transaction.atomic():
        stale = SpreadBar.objects.filter(pair=pair)
        if since is not None:
            stale = stale.filter(ts__gte=since)
        stale.delete()
        SpreadBar.objects.bulk_create(rows, batch_size=500)
    return len(rows)
