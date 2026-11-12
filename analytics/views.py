"""Страница аналитики контрактов и её JSON.

Выбор: пара, ряд (сырой или очищенный), месяц контракта или все, исключённые
контракты и параметры сделки. Ответ: линии контрактов по дням до склейки,
три сетки «вход × выход» и лучшие диапазоны. Расчёт кэшируется, пока у пары
не появились новые бары.
"""

from collections import OrderedDict

from django.db.models import Count, Max
from django.http import JsonResponse
from django.shortcuts import render

from analytics import core
from analytics.data import MONTH_NAMES, MONTHS, contracts
from analytics.history import refresh, stale
from analytics.models import HistoryBar
from market.models import Pair
from strategy.models import SpreadBar
from strategy.rebuild import history_span


def period_days(pair):
    """Один период контракта в днях: месяц у нефти и газа, квартал у остальных.

    История в HistoryBar — два периода до склейки (history_span), период — половина.
    """
    return (history_span(pair).days + 1) // 2

CACHE_SIZE = 24
_cache = OrderedDict()


def _pair(code):
    pairs = Pair.objects.filter(active=True)
    return pairs.filter(code=code).first() if code else pairs.first()


def _int(request, name, default, low, high):
    try:
        return max(low, min(high, int(request.GET.get(name, default))))
    except (TypeError, ValueError):
        return default


def _float(request, name, default, low, high):
    try:
        return max(low, min(high, float(request.GET.get(name, default))))
    except (TypeError, ValueError):
        return default


def page(request):
    return render(request, "analytics/analytics.html", {
        "pairs": Pair.objects.filter(active=True).order_by("code"),
        "pair": request.GET.get("pair", ""),
    })


def contracts_json(request):
    """Контракты пары для выбора месяца и исключений."""
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return JsonResponse({"pair": "", "months": [], "contracts": []})
    found = contracts(pair)
    months = sorted({item["month"] for item in found if item["month"]}, key=MONTHS.index)
    return JsonResponse({
        "pair": pair.code, "title": pair.title,
        "months": [{"code": month, "name": MONTH_NAMES[MONTHS.index(month)]} for month in months],
        "contracts": [
            {"code": item["code"], "month": item["month"], "year": item["year"], "live": item["live"],
             "days": len(item["points"])}
            for item in found
        ],
    })


def analytics_json(request):
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return JsonResponse({"error": "нет пары"}, status=404)
    params = {
        "series": "rate" if request.GET.get("series") == "rate" else "raw",
        "month": request.GET.get("month", "") if request.GET.get("month", "") in MONTHS else "",
        "exclude": sorted({code for code in request.GET.get("exclude", "").split(",") if code}),
        "hold": _int(request, "hold", 10, 1, 120),
        "years": _int(request, "years", 2, 1, 20),
        "smooth": _int(request, "smooth", 5, 0, 20),
        "max_days": _int(request, "max_days", 0, 0, 400),
        "periods": _int(request, "periods", 2, 1, 2),
        "capital": _float(request, "capital", 100.0, 1.0, 1000.0),
        "cost": _float(request, "cost", 0.0, 0.0, 1e9),
        "metric": request.GET.get("metric") if request.GET.get("metric") in ("ret", "ann", "ratio") else "ret",
    }
    if stale(pair):
        refresh(pair)
    bars = HistoryBar.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("id"))
    spread = SpreadBar.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("id"))
    key = (pair.id, bars["n"], bars["last"], spread["n"], spread["last"], repr(sorted(params.items())))
    if key in _cache:
        _cache.move_to_end(key)
        return JsonResponse(_cache[key])
    body = _compute(pair, params)
    _cache[key] = body
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return JsonResponse(body)


def _compute(pair, params):
    found = contracts(pair, params["series"])
    chosen = [item for item in found if not params["month"] or item["month"] == params["month"]]
    excluded = set(params["exclude"])
    used = [item for item in chosen if not item["live"] and item["code"] not in excluded and item["points"]]
    longest = max((point[0] for item in chosen for point in item["points"]), default=0)
    # Глубина: 1 период (квартал или месяц до склейки) или 2; «дней ≤» режет ещё сильнее.
    period = period_days(pair)
    limit = period * params["periods"]
    if params["max_days"]:
        limit = min(limit, params["max_days"])
    width = min(longest, limit) + 1
    lines = [
        {
            "code": item["code"], "year": item["year"], "month": item["month"], "live": item["live"],
            "excluded": item["code"] in excluded,
            "x": [point[0] for point in item["points"] if point[0] < width],
            "y": [point[1] for point in item["points"] if point[0] < width],
        }
        for item in chosen
    ]
    body = {
        "pair": pair.code, "title": pair.title, "quote": pair.quote, "absolute": pair.absolute_levels,
        "params": params, "width": width, "period_days": period,
        "lines": lines, "used": [item["code"] for item in used],
        "years": sorted({item["year"] for item in used}),
    }
    if len(used) < params["years"] or width <= params["hold"]:
        body["note"] = (
            f"мало истории: истёкших контрактов {len(used)}, нужно не меньше {params['years']}"
            if len(used) < params["years"] else f"окно {width} дн. не длиннее удержания {params['hold']} дн."
        )
        return core.clean(body)
    S, P = core.matrix(used, width)
    G, best = core.returns_grid(
        S, P, params["hold"], params["capital"], params["cost"], params["years"], params["smooth"], params["metric"],
    )
    M, best_move = core.move_grid(S, params["hold"], params["years"], params["smooth"])
    D, best_side = core.side_grid(S, params["hold"], params["years"], params["smooth"])
    body.update({
        "returns": {key: G[key] for key in ("smooth", "ret", "ann", "worst", "win", "mae", "ratio", "n", "side")},
        "move": {key: M[key] for key in ("smooth", "move", "agree", "raw", "n")},
        "direction": {key: D[key] for key in ("smooth", "move", "agree", "raw", "n")},
        "best": {"returns": best, "move": best_move, "direction": best_side},
        "ranges": core.best_ranges(G),
    })
    return core.clean(body)
