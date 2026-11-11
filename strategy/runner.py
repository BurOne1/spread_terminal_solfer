"""Общий проход стратегии: бары из базы, уровни и настройки от вызывающего.

И веб-график, и run_strategy зовут одно и то же, чтобы марка
открытой позиции считалась одинаково. PNL в базу не пишется:
у каждого пользователя свои уровни, график считает его на лету.
"""

from market.models import Pair
from strategy.engine import pair_levels, run
from strategy.models import SpreadBar

BASES = ("close", "open", "mid")
ROW_FIELDS = (
    "ts", "contract__code", "raw_spread", "spread_rate", "raw_open", "rate_open",
    "days_left", "spot", "fair", "future_close", "perp_close",
)


def priced(bar, basis="close"):
    """Сырой спред и spread_rate бара по закрытию, открытию или (O+C)/2.

    Внутри часа срок и ставки одни и те же, поэтому середина
    spread_rate — точное среднее открытия и закрытия.
    """
    raw = float(bar.raw_spread)
    rate = None if bar.spread_rate is None else float(bar.spread_rate)
    if basis not in ("open", "mid") or bar.raw_open is None:
        return raw, rate
    raw_open = float(bar.raw_open)
    rate_open = None if bar.rate_open is None else float(bar.rate_open)
    if basis == "open":
        return raw_open, rate_open
    mid_rate = None if rate is None or rate_open is None else (rate + rate_open) / 2
    return (raw + raw_open) / 2, mid_rate


def spread_rows(pair):
    """Бары пары кортежами по порядку времени. Модель на каждый бар заметно дороже."""
    return list(SpreadBar.objects.filter(pair=pair).order_by("ts").values_list(*ROW_FIELDS, named=True))


def bars_of(pair, basis="close", rows=None):
    """rows — уже прочитанные spread_rows, чтобы не читать бары второй раз."""
    out = []
    for bar in spread_rows(pair) if rows is None else rows:
        raw, rate = priced(bar, basis)
        row = {
            "ts": bar.ts,
            "contract": bar.contract__code,
            "raw": raw,
            "rate": rate,
            "spot": float(bar.spot),
            "fair": None if bar.fair is None else float(bar.fair),
            "future": float(bar.future_close),
            "perp": float(bar.perp_close),
        }
        if pair.foreign_source == "TRI":
            row["third"] = float(bar.future_close)
        out.append(row)
    return out


def funding_of(pair):
    """Ставки фандинга. У треугольника — фандинг вечного металла на один контракт.

    В базе золото — рубли на контракт 1 грамм, серебро — на контракт 100 граммов.
    Контракт TRIGD это 1 унция, TRISV — 10 унций.
    Лонг спреда шортит вечный, поэтому знак тот же, что у обычной пары.
    У календаря обе ноги квартальные, у BR − BZ обе месячные: фандинга нет.
    """
    from rates.funding import funding_field
    from rates.models import FundingDay
    if pair.calendar or pair.zero_fair:
        return [], "daily"
    if pair.foreign_source == "TRI":
        from strategy.rebuild import OZ_GRAMS, TRI_PERP, TRI_SPOT
        if pair.tri_code == "TRIGL" or pair.tri_code in TRI_SPOT:
            return [], "daily"
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.tri_code, "")).first()
        field = funding_field(metal)
        ounces = pair.lot or 1
        per_gram = 100 if field == "swaprate_silver" else 1
        scale = ounces * OZ_GRAMS / per_gram
        rows = [
            (day, float(value) * scale)
            for day, value in FundingDay.objects.order_by("date").values_list("date", field)
            if value is not None
        ]
        return rows, "daily"
    if pair.foreign_source == "EWMA14":
        from rates.models import FundingPoint
        rows = [
            (ts, float(rate))
            for ts, rate in FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
        ]
        return rows, "interval"
    field = funding_field(pair)
    rows = [
        (day, float(value))
        for day, value in FundingDay.objects.order_by("date").values_list("date", field)
        if value is not None
    ]
    return rows, "daily"


def notional_of(pair):
    if pair.foreign_source == "TRI":
        from strategy.rebuild import OZ_GRAMS
        return (pair.lot or 1) * OZ_GRAMS
    return pair.lot or 1000


def pass_pair(pair, entry_mode="touch", average=True, levels=None, basis="close", rows=None):
    """levels: (лонг, шорт, тейки лонга, тейки шорта) в долях спота со знаком.

    Пусто — уровни, сохранённые у пары, они же значения по умолчанию.
    Возвращает точки PNL, закрытые сделки и открытую позицию или None.
    """
    funding, mode = funding_of(pair)
    long_pcts, short_pcts, long_tp, short_tp = levels or pair_levels(pair)
    return run(
        bars_of(pair, basis, rows), funding,
        entry_mode=entry_mode,
        average=average,
        notional=notional_of(pair),
        funding_mode=mode,
        long_pcts=long_pcts,
        short_pcts=short_pcts,
        long_tp=long_tp,
        short_tp=short_tp,
        **entry_rules(pair),
    )


def entry_rules(pair):
    """У BR − BZ fair = 0 и спред в центах: правило знака и рублёвый порог не подходят."""
    if pair.zero_fair:
        return {"long_needs_sign": False, "min_spread": 0.0}
    return {}