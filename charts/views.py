from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import json

from market.models import Candle, Instrument, Pair
from strategy.models import EquityPoint, SpreadBar


def _pair(code):
    pairs = Pair.objects.filter(active=True)
    if code:
        return pairs.filter(code=code).first() or pairs.first()
    return pairs.first()


def _contracts(pair):
    if pair is None:
        return []
    return list(
        Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, roll_at__isnull=False)
        .order_by("roll_at")
        .values_list("code", flat=True)
    )


def _points(pair, start="", end=""):
    if pair is None:
        return []
    codes = _contracts(pair)
    if start and end and start in codes and end in codes:
        left, right = codes.index(start), codes.index(end)
        if left > right:
            left, right = right, left
        codes = codes[left:right + 1]
    bars = SpreadBar.objects.filter(pair=pair).select_related("contract").order_by("ts")
    if start or end:
        bars = bars.filter(contract__code__in=codes)
    scale = pair.future_scale or 1
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    perp_ohlc = {}
    if perp is not None:
        perp_ohlc = {
            ts: (float(open_), float(high), float(low), float(close))
            for ts, open_, high, low, close in Candle.objects.filter(instrument=perp).values_list(
                "ts", "open", "high", "low", "close"
            )
        }
    future_ohlc = {}
    for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, code__in=codes):
        for ts, open_, high, low, close in Candle.objects.filter(instrument=instrument).values_list(
            "ts", "open", "high", "low", "close"
        ):
            future_ohlc[ts] = (float(open_) / scale, float(high) / scale, float(low) / scale, float(close) / scale)
    rows = []
    for bar in bars:
        raw = float(bar.raw_spread)
        candle = {"o": raw, "h": raw, "l": raw, "c": raw}
        future = future_ohlc.get(bar.ts)
        perp_bar = perp_ohlc.get(bar.ts)
        if pair.foreign_source != "TRI" and future and perp_bar:
            opened = future[0] - perp_bar[0]
            closed = future[3] - perp_bar[3]
            high = max(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
            low = min(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
            candle = {"o": opened, "h": high, "l": low, "c": closed}
        rows.append({
            "t": bar.ts.isoformat(),
            "contract": bar.contract.code,
            "raw": raw,
            "rate": None if bar.spread_rate is None else float(bar.spread_rate),
            "days": bar.days_left,
            "price": float(bar.spot or bar.perp_close or 0),
            "candle": candle,
        })
    return rows


def _pnl(pair, start="", end=""):
    if pair is None:
        return []
    codes = _contracts(pair)
    if start and end and start in codes and end in codes:
        left, right = codes.index(start), codes.index(end)
        if left > right:
            left, right = right, left
        codes = codes[left:right + 1]
    rows = EquityPoint.objects.filter(pair=pair).order_by("ts")
    if start or end:
        rows = rows.filter(contract__in=codes)
    return [
        {
            "t": row.ts.isoformat(),
            "contract": row.contract,
            "equity": float(row.equity),
            "marker": row.marker,
        }
        for row in rows
    ]


def _levels(pair):
    from strategy.engine import pair_levels
    empty = {
        "long": [], "short": [], "long_tp": [], "short_tp": [],
        "long_pcts": [], "short_pcts": [], "long_tp_pcts": [], "short_tp_pcts": [],
        "pcts": [], "tp_pcts": [], "tp": [], "spot": None,
    }
    if pair is None:
        return empty
    long_pcts, short_pcts, long_tp, short_tp = pair_levels(pair)
    last = _level_spot(pair)
    payload = {
        "long_pcts": long_pcts,
        "short_pcts": short_pcts,
        "long_tp_pcts": long_tp,
        "short_tp_pcts": short_tp,
        "pcts": long_pcts,
        "tp_pcts": long_tp,
        "spot": last,
    }
    if last is None:
        payload.update({"long": [], "short": [], "long_tp": [], "short_tp": [], "tp": []})
        return payload
    price = float(last)
    payload.update({
        "long": [price * pct for pct in long_pcts],
        "short": [price * pct for pct in short_pcts],
        "long_tp": [price * pct for pct in long_tp],
        "short_tp": [price * pct for pct in short_tp],
        "tp": [price * pct for pct in long_tp] + [price * pct for pct in short_tp],
    })
    return payload


def _level_spot(pair):
    """Цена, от которой уровни — проценты. У треугольника это вечный металл, руб/грамм."""
    if pair.foreign_source == "TRI":
        from strategy.rebuild import TRI_PERP
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.code, "")).first()
        if metal is None:
            return None
        perp = Instrument.objects.filter(pair=metal, code=metal.perp_code).first()
    else:
        perp = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
        if perp is None:
            perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None:
        return None
    last = Candle.objects.filter(instrument=perp).order_by("-ts").values_list("close", flat=True).first()
    return None if last is None else float(last)


def _funding(pair):
    if pair is not None and pair.foreign_source == "EWMA14":
        from rates.models import FundingPoint
        return [
            {"d": ts.date().isoformat(), "v": float(rate)}
            for ts, rate in FundingPoint.objects.filter(pair=pair).order_by("ts").values_list("ts", "rate")
        ]
    from rates.models import FundingDay
    from rates.funding import funding_field
    if pair is not None and pair.foreign_source == "TRI":
        from strategy.rebuild import TRI_PERP
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.code, "")).first()
        field = funding_field(metal)
    else:
        field = funding_field(pair)
    return [
        {"d": day.isoformat(), "v": float(value)}
        for day, value in FundingDay.objects.order_by("date").values_list("date", field)
        if value is not None
    ]


def _funding_scale(pair):
    """Во сколько единиц спреда записан один фандинг.

    Валюта — за контракт 1000, золото — за грамм, серебро — за 100 грамм.
    Треугольник берёт шкалу своего металла, чтобы фандинг и контанго
    были в одних рублях за грамм.
    """
    if pair is None:
        return 1000
    if pair.foreign_source == "TRI":
        return 100 if pair.code == "TRISV" else 1
    if pair.code == "SILVER":
        return 100
    if pair.code == "GOLD":
        return 1
    return pair.lot or 1000


def home(request):
    pairs = list(Pair.objects.filter(active=True).order_by("code"))
    pair = _pair(request.GET.get("pair", ""))
    contracts = _contracts(pair)
    start = request.GET.get("from", "")
    end = request.GET.get("to", "")
    if start not in contracts:
        start = contracts[0] if contracts else ""
    if end not in contracts:
        end = contracts[-1] if contracts else ""
    points = _points(pair, start, end)
    pnl = _pnl(pair, start, end)
    funding = _funding(pair)
    return render(request, "charts/home.html", {
        "pairs": pairs,
        "pair": pair,
        "contracts": contracts,
        "start": start,
        "end": end,
        "points": points,
        "pnl": pnl,
        "funding": funding,
        "funding_scale": _funding_scale(pair),
        "funding_kind": "rate" if pair is not None and pair.foreign_source == "EWMA14" else "ruble",
        "levels": _levels(pair),
        "count": len(points),
    })


def _rate_label(pair):
    if pair is None:
        return ""
    if pair.foreign_source == "TRI":
        return "треугольник, RUONIA и фандинг вечного"
    if pair.foreign_source == "EWMA14":
        return "EWMA14 фандинга"
    return pair.foreign_source


def spread_json(request):
    pair = _pair(request.GET.get("pair", ""))
    from strategy.runner import refresh_pnl
    refresh_pnl(pair)
    contracts = _contracts(pair)
    start = request.GET.get("from", "")
    end = request.GET.get("to", "")
    if start not in contracts:
        start = contracts[0] if contracts else ""
    if end not in contracts:
        end = contracts[-1] if contracts else ""
    points = _points(pair, start, end)
    funding = _funding(pair)
    return JsonResponse({
        "pair": "" if pair is None else pair.code,
        "title": "" if pair is None else (
            pair.title if pair.foreign_source == "TRI" else f"{pair.future_root} − {pair.perp_code}"
        ),
        "spot": "" if pair is None else pair.spot_code,
        "rate": _rate_label(pair),
        "quote": "" if pair is None else pair.quote,
        "root": "" if pair is None else pair.future_root,
        "contracts": contracts,
        "from": start,
        "to": end,
        "count": len(points),
        "points": points,
        "funding": funding,
        "funding_scale": _funding_scale(pair),
        "funding_kind": "rate" if pair is not None and pair.foreign_source == "EWMA14" else "ruble",
        "pnl": _pnl(pair, start, end),
        "levels": _levels(pair),
    })


@csrf_exempt
@require_POST
def save_levels(request):
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return JsonResponse({"error": "нет пары"}, status=404)
    body = json.loads(request.body or "{}")

    def signed(key):
        return {"signed": True, "values": [float(item) / 100 for item in body.get(key, [])]}

    pair.long_levels = signed("long_entries")
    pair.short_levels = signed("short_entries")
    pair.long_tp = signed("long_takes")
    pair.short_tp = signed("short_takes")
    pair.save(update_fields=["long_levels", "short_levels", "long_tp", "short_tp"])
    return JsonResponse({"ok": True, "levels": _levels(pair)})


@csrf_exempt
@require_POST
def recalc(request):
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return JsonResponse({"error": "нет пары"}, status=404)
    from strategy.runner import pass_pair, store_points
    body = json.loads(request.body or "{}")
    points, trades = pass_pair(
        pair,
        entry_mode=body.get("entry", "touch"),
        average=bool(body.get("average", True)),
    )
    store_points(pair, points)
    return JsonResponse({"ok": True, "trades": len(trades), "pnl": points})