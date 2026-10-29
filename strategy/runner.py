"""Общий проход стратегии: бары из базы, уровни пары, запись PNL.

И веб-график, и run_strategy зовут одно и то же, чтобы марка
открытой позиции считалась одинаково и обновлялась с новыми барами.
"""

from decimal import Decimal

from market.models import Pair
from strategy.engine import pair_levels, run
from strategy.models import EquityPoint, SpreadBar


def bars_of(pair):
    rows = []
    for bar in SpreadBar.objects.filter(pair=pair).select_related("contract").order_by("ts"):
        row = {
            "ts": bar.ts,
            "contract": bar.contract.code,
            "raw": float(bar.raw_spread),
            "rate": None if bar.spread_rate is None else float(bar.spread_rate),
            "spot": float(bar.spot),
            "fair": None if bar.fair is None else float(bar.fair),
            "future": float(bar.future_close),
            "perp": float(bar.perp_close),
        }
        if pair.foreign_source == "TRI":
            row["third"] = float(bar.future_close)
        rows.append(row)
    return rows


def funding_of(pair):
    """Ставки фандинга. У треугольника — фандинг вечного металла на один контракт.

    В базе золото — рубли за грамм, серебро — за 100 грамм.
    Контракт TRIGD это 1 унция, TRISV — 10 унций.
    Лонг спреда шортит вечный, поэтому знак тот же, что у обычной пары.
    """
    from rates.funding import funding_field
    from rates.models import FundingDay
    if pair.foreign_source == "TRI":
        from strategy.rebuild import OZ_GRAMS, TRI_PERP
        if pair.code == "TRIGL":
            return [], "daily"
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.code, "")).first()
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


def pass_pair(pair, entry_mode="touch", average=True):
    funding, mode = funding_of(pair)
    long_pcts, short_pcts, long_tp, short_tp = pair_levels(pair)
    return run(
        bars_of(pair), funding,
        entry_mode=entry_mode,
        average=average,
        notional=notional_of(pair),
        funding_mode=mode,
        long_pcts=long_pcts,
        short_pcts=short_pcts,
        long_tp=long_tp,
        short_tp=short_tp,
    )


def store_points(pair, points):
    EquityPoint.objects.filter(pair=pair).delete()
    EquityPoint.objects.bulk_create([
        EquityPoint(
            pair=pair,
            ts=point["t"],
            contract=point["contract"],
            equity=Decimal(f"{point['equity']:.2f}"),
            marker=point["marker"],
        )
        for point in points
    ], batch_size=1000)


def refresh_pnl(pair):
    """Пересчёт, если появились бары новее последней точки PNL."""
    if pair is None:
        return
    last_bar = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
    last_pnl = EquityPoint.objects.filter(pair=pair).order_by("-ts").values_list("ts", flat=True).first()
    if last_bar is None or last_pnl == last_bar:
        return
    points, _trades = pass_pair(pair)
    store_points(pair, points)