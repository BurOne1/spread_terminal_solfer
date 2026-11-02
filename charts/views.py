"""Страница графика и её JSON.

Уровни, режим входа и база цены живут в браузере у каждого пользователя
и приходят параметрами запроса. Стратегия считается на лету и в базу
ничего не пишет. Без параметров берутся уровни, сохранённые у пары.
"""

from django.http import JsonResponse
from django.shortcuts import render

from market.models import Candle, Instrument, Pair
from strategy.models import SpreadBar
from strategy.runner import BASES, priced


def _pair(code):
    pairs = Pair.objects.filter(active=True)
    if code:
        return pairs.filter(code=code).first() or pairs.first()
    return pairs.first()


def _contracts(pair):
    if pair is None:
        return []
    owner = pair
    if pair.code == "TRIGL":
        owner = Pair.objects.filter(code="GOLD").first() or pair
    return list(
        Instrument.objects.filter(pair=owner, kind=Instrument.Kind.FUTURE, roll_at__isnull=False)
        .order_by("roll_at")
        .values_list("code", flat=True)
    )


def _window(codes, start, end):
    if start and end and start in codes and end in codes:
        left, right = codes.index(start), codes.index(end)
        if left > right:
            left, right = right, left
        return codes[left:right + 1]
    return codes


def _ohlc(instrument, since, scale=1):
    rows = Candle.objects.filter(instrument=instrument)
    if since is not None:
        rows = rows.filter(ts__gte=since)
    return {
        ts: (float(open_) / scale, float(high) / scale, float(low) / scale, float(close) / scale)
        for ts, open_, high, low, close in rows.values_list("ts", "open", "high", "low", "close")
    }


def _points(pair, start="", end="", basis="close"):
    if pair is None:
        return []
    codes = _window(_contracts(pair), start, end)
    bars = list(
        SpreadBar.objects.filter(pair=pair, contract__code__in=codes).select_related("contract").order_by("ts")
    )
    if not bars:
        return []
    since = bars[0].ts
    perp_ohlc = {}
    future_ohlc = {}
    if pair.foreign_source != "TRI":
        scale = pair.future_scale or 1
        perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
        if perp is not None:
            perp_ohlc = _ohlc(perp, since)
        for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, code__in=codes):
            future_ohlc.update(_ohlc(instrument, since, scale))
    rows = []
    for bar in bars:
        raw, rate = priced(bar, basis)
        future = future_ohlc.get(bar.ts)
        perp_bar = perp_ohlc.get(bar.ts)
        if future and perp_bar:
            opened = future[0] - perp_bar[0]
            closed = future[3] - perp_bar[3]
            high = max(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
            low = min(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
        else:
            closed = float(bar.raw_spread)
            opened = closed if bar.raw_open is None else float(bar.raw_open)
            high, low = max(opened, closed), min(opened, closed)
        rows.append({
            "t": bar.ts.isoformat(),
            "contract": bar.contract.code,
            "raw": raw,
            "rate": rate,
            "days": bar.days_left,
            "price": float(bar.spot or bar.perp_close or 0),
            "candle": {"o": opened, "h": high, "l": low, "c": closed},
        })
    return rows


def _percents(request, key):
    """Список процентов из параметра запроса. Нет параметра — None."""
    if key not in request.GET:
        return None
    values = []
    for item in request.GET.get(key, "").split(","):
        item = item.strip().replace(",", ".")
        if not item:
            continue
        try:
            values.append(float(item) / 100)
        except ValueError:
            continue
    return values


def _user_levels(request, pair):
    """Уровни из браузера: доли спота со знаком. Не прислал ни одного — уровни пары."""
    from strategy.engine import pair_levels
    sent = [_percents(request, key) for key in ("long", "short", "ltp", "stp")]
    if all(item is None for item in sent):
        return pair_levels(pair)
    return tuple(item or [] for item in sent)


def _settings(request):
    basis = request.GET.get("basis", "close")
    return {
        "entry_mode": "reverse" if request.GET.get("entry") == "reverse" else "touch",
        "average": request.GET.get("average", "1") != "0",
        "basis": basis if basis in BASES else "close",
    }


def _strategy(pair, request, codes):
    """PNL, число сделок и открытая позиция для выбранного окна контрактов."""
    from strategy.runner import pass_pair
    options = _settings(request)
    points, trades, opened = pass_pair(pair, levels=_user_levels(request, pair), **options)
    allowed = set(codes)
    pnl = [point for point in points if point["contract"] in allowed]
    stamps = {point["t"] for point in pnl}
    closed = sum(1 for trade in trades if trade["exit"].isoformat() in stamps)
    summary = None
    if opened is not None:
        summary = {
            "side": opened["side"],
            "n": opened["n"],
            "contract": opened["contract"],
            "entry": opened["entry"].isoformat() if opened["entry"] else "",
            "price": opened["price"],
            "fund": opened["fund"],
        }
    return pnl, closed, summary


def _levels(pair, chosen=None):
    from strategy.engine import pair_levels
    empty = {
        "long": [], "short": [], "long_tp": [], "short_tp": [],
        "long_pcts": [], "short_pcts": [], "long_tp_pcts": [], "short_tp_pcts": [],
        "pcts": [], "tp_pcts": [], "tp": [], "spot": None,
    }
    if pair is None:
        return empty
    long_pcts, short_pcts, long_tp, short_tp = chosen or pair_levels(pair)
    last = _level_spot(pair)
    payload = {
        "long_pcts": list(long_pcts),
        "short_pcts": list(short_pcts),
        "long_tp_pcts": list(long_tp),
        "short_tp_pcts": list(short_tp),
        "pcts": list(long_pcts),
        "tp_pcts": list(long_tp),
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
        if pair.code == "TRIGL":
            return []
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

    Валюта — за контракт 1000, золото — за грамм, серебро — за контракт
    100 граммов при цене за грамм. Треугольник берёт шкалу своего металла,
    чтобы фандинг и контанго были в одних рублях за грамм.
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


def _range(request, contracts):
    start = request.GET.get("from", "")
    end = request.GET.get("to", "")
    if start not in contracts:
        start = contracts[0] if contracts else ""
    if end not in contracts:
        end = contracts[-1] if contracts else ""
    return start, end


def home(request):
    """Каркас страницы. Данные с настройками браузера приходят первым же /api/spread/."""
    pairs = list(Pair.objects.filter(active=True).order_by("code"))
    pair = _pair(request.GET.get("pair", ""))
    contracts = _contracts(pair)
    start, end = _range(request, contracts)
    return render(request, "charts/home.html", {
        "pairs": pairs,
        "pair": pair,
        "contracts": contracts,
        "start": start,
        "end": end,
        "points": [],
        "pnl": [],
        "funding": [],
        "funding_scale": _funding_scale(pair),
        "funding_kind": "rate" if pair is not None and pair.foreign_source == "EWMA14" else "ruble",
        "levels": _levels(pair),
        "count": 0,
    })


def _title(pair):
    if pair is None:
        return ""
    return pair.title if pair.foreign_source == "TRI" else f"{pair.future_root} − {pair.perp_code}"


def watchlist_json(request):
    """Последний бар каждой активной пары и изменение spread_rate за сутки."""
    from datetime import timedelta
    basis = _settings(request)["basis"]
    rows = []
    for pair in Pair.objects.filter(active=True).order_by("code"):
        bars = SpreadBar.objects.filter(pair=pair).select_related("contract").order_by("-ts")
        last = bars.first()
        item = {"code": pair.code, "title": _title(pair), "name": pair.title, "venue": pair.venue}
        if last is not None:
            raw, rate = priced(last, basis)
            before = bars.filter(ts__lte=last.ts - timedelta(days=1)).first()
            old_rate = None if before is None else priced(before, basis)[1]
            item.update({
                "t": last.ts.isoformat(),
                "contract": last.contract.code,
                "raw": raw,
                "rate": rate,
                "days": last.days_left,
                "change": None if rate is None or old_rate is None else rate - old_rate,
            })
        rows.append(item)
    return JsonResponse({"basis": basis, "pairs": rows})


def _rate_label(pair):
    if pair is None:
        return ""
    if pair.foreign_source == "TRI":
        return "треугольник, ноги чистятся отдельно" if pair.code == "TRIGL" else "треугольник, RUONIA и фандинг вечного"
    if pair.foreign_source == "EWMA14":
        return "EWMA14 фандинга"
    return pair.foreign_source


def spread_json(request):
    pair = _pair(request.GET.get("pair", ""))
    contracts = _contracts(pair)
    start, end = _range(request, contracts)
    options = _settings(request)
    points = _points(pair, start, end, options["basis"])
    pnl, trades, opened = [], 0, None
    if pair is not None:
        pnl, trades, opened = _strategy(pair, request, _window(contracts, start, end))
    return JsonResponse({
        "pair": "" if pair is None else pair.code,
        "title": _title(pair),
        "venue": "" if pair is None else pair.venue,
        "spot": "" if pair is None else pair.spot_code,
        "rate": _rate_label(pair),
        "quote": "" if pair is None else pair.quote,
        "root": "" if pair is None else pair.future_root,
        "contracts": contracts,
        "from": start,
        "to": end,
        "basis": options["basis"],
        "count": len(points),
        "points": points,
        "funding": _funding(pair),
        "funding_scale": _funding_scale(pair),
        "funding_kind": "rate" if pair is not None and pair.foreign_source == "EWMA14" else "ruble",
        "pnl": pnl,
        "trades": trades,
        "position": opened,
        "levels": _levels(pair, None if pair is None else _user_levels(request, pair)),
        "pair_levels": _levels(pair),
    })
