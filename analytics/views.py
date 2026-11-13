"""Страница аналитики контрактов и её JSON.

Выбор: пара, ряд (сырой или очищенный), месяц контракта или все, исключённые
контракты и параметры сделки. Ответ: линии контрактов по дням до склейки,
три сетки «вход × выход» и лучшие диапазоны. Расчёт кэшируется, пока у пары
не появились новые бары.
"""

import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from django.db.models import Count, Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from analytics import core
from analytics.data import MONTH_NAMES, MONTHS, contracts
from analytics.history import refresh, stale
from analytics.models import HistoryBar
from analytics.xlsx import workbook
from charts.views import last_pair_code
from market.models import Pair
from strategy.models import SpreadBar
from strategy.rebuild import history_span


def period_days(pair):
    """Один период контракта в днях: месяц у нефти и газа, квартал у остальных.

    История в HistoryBar — два периода до склейки (history_span), период — половина.
    """
    return (history_span(pair).days + 1) // 2

# Кэш — готовый JSON в байтах, а не словари: при окне 184 дня расчёт в объектах
# Python — десятки МБ, в JSON — ~6 МБ. Лимит по объёму, у каждого процесса gunicorn свой.
CACHE_SIZE = 24
CACHE_BYTES = 48 * 1024 * 1024
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
        "pair": last_pair_code(request),
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


def _params(request):
    return {
        "series": "rate" if request.GET.get("series") == "rate" else "raw",
        "month": request.GET.get("month", "") if request.GET.get("month", "") in MONTHS else "",
        "exclude": sorted({code for code in request.GET.get("exclude", "").split(",") if code}),
        "hold": _int(request, "hold", 10, 1, 120),
        "years": _int(request, "years", 2, 1, 20),
        "smooth": _int(request, "smooth", 5, 0, 20),
        "max_days": _int(request, "max_days", 0, 0, 400),
        "periods": _int(request, "periods", 2, 1, 2),
        "share": _int(request, "share", 35, 5, 100),
        "capital": _float(request, "capital", 100.0, 1.0, 1000.0),
        "cost": _float(request, "cost", 0.0, 0.0, 1e9),
        "metric": request.GET.get("metric") if request.GET.get("metric") in ("ret", "ann", "ratio") else "ret",
    }


def _encoded(pair, params):
    """JSON расчёта (bytes) с кэшем, пока у пары не появились новые бары."""
    if stale(pair):
        refresh(pair)
    bars = HistoryBar.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("id"))
    spread = SpreadBar.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("id"))
    key = (pair.id, bars["n"], bars["last"], spread["n"], spread["last"], repr(sorted(params.items())))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    data = json.dumps(_compute(pair, params), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _cache[key] = data
    while len(_cache) > 1 and (len(_cache) > CACHE_SIZE or sum(map(len, _cache.values())) > CACHE_BYTES):
        _cache.popitem(last=False)
    return data


def _body(pair, params):
    return json.loads(_encoded(pair, params))


def analytics_json(request):
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return JsonResponse({"error": "нет пары"}, status=404)
    return HttpResponse(_encoded(pair, _params(request)), content_type="application/json")


METRIC_NAMES = {"ret": "% за период", "ann": "% годовых", "ratio": "доход / разброс"}


def _span(pair_of_days):
    low, high = pair_of_days
    return f"{high}–{low}" if low != high else str(low)


def _dates(pair_of_dates):
    low, high = (datetime.fromisoformat(value).strftime("%d.%m.%Y") for value in pair_of_dates)
    return low if low == high else f"{low} – {high}"


def analytics_xlsx(request):
    """Таблица зон и параметры расчёта в .xlsx — те же параметры, что у страницы."""
    pair = _pair(request.GET.get("pair", ""))
    if pair is None:
        return HttpResponse("нет пары", status=404, content_type="text/plain; charset=utf-8")
    params = _params(request)
    body = _body(pair, params)
    header = ["№", "сторона", "покупка, дн. до эксп.", "продажа, дн. до эксп.", "держим, дн.", "клеток",
              "лучшая: вход за, дн.", "лучшая: выход за, дн.", METRIC_NAMES[params["metric"]] + " (сглаж.)",
              "% за период", "% годовых", "худший год, % год.", "в плюс, %", "MAE, % капитала", "доход / разброс",
              "направление, % лет", "z (медиана)", "сужение к нулю, масштаба", "лет"]
    target = body.get("target")
    if target:
        header[4:4] = [f"покупка, даты ({target['code']})", f"продажа, даты ({target['code']})"]
    rows = [header]
    for number, zone in enumerate(body.get("ranges") or [], 1):
        pct = (lambda value: None if value is None else round(value * 100, 1))
        rows.append([
            number, zone["side"], _span(zone["buy"]), _span(zone["sell"]), _span(zone["hold"]), zone["cells"],
            zone["best"][0], zone["best"][1], zone["metric"], zone["ret"], zone["ann"], zone["worst"],
            pct(zone["win"]), zone["mae"], zone["ratio"], pct(zone["agree"]), zone["z"], zone["narrowing"], zone["n"],
        ])
        if target:
            rows[-1][4:4] = [_dates(zone["buy_dates"]), _dates(zone["sell_dates"])]
    if len(rows) == 1:
        rows.append(["", body.get("note") or "зон нет"])
    settings = [["параметр", "значение"],
                ["пара", f"{pair.code} · {pair.title}"],
                ["ряд", "очищенный" if params["series"] == "rate" else "сырой"],
                ["месяц контракта", params["month"] or "все"],
                ["контракты в статистике", ", ".join(body.get("used") or [])],
                ["исключены", ", ".join(params["exclude"])],
                ["окно, дн. до склейки", body.get("width", 1) - 1],
                ["склейка для дат", "" if not body.get("target") else
                 f"{body['target']['code']} {datetime.fromisoformat(body['target']['roll']):%d.%m.%Y}"
                 + (" (оценка по прошлому году)" if body["target"]["estimated"] else "")],
                ["удержание ≥, дн.", params["hold"]], ["лет ≥", params["years"]], ["сглаживание ±, дн.", params["smooth"]],
                ["метрика", METRIC_NAMES[params["metric"]]], ["порог зоны, % лучшей", params["share"]],
                ["капитал, % цены", params["capital"]], ["издержки за круг", params["cost"]],
                ["дни", "диапазоны «покупка» и «продажа» — дней до экспирации, от дальнего к ближнему"]]
    data = workbook([
        ("Зоны", rows, [5, 8, 14, 14, 12, 8, 12, 12, 16, 11, 11, 14, 10, 12, 12, 14, 11, 16, 6]),
        ("Параметры", settings, [26, 60]),
    ])
    response = HttpResponse(
        data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="analytics_{pair.code}.xlsx"'
    return response


def target_roll(chosen, now=None):
    """Склейка ближайшего контракта выбранного месяца: (код, дата, оценка ли).

    Идущий контракт есть в базе — его склейка. Нет — последняя прошедшая
    склейка этого месяца, сдвинутая на нужное число лет (оценка ±несколько дней).
    """
    now = now or datetime.now(timezone.utc)
    ahead = sorted((item for item in chosen if item["roll_at"] and item["roll_at"] > now), key=lambda item: item["roll_at"])
    if ahead:
        return ahead[0]["code"], ahead[0]["roll_at"].date(), False
    past = sorted((item for item in chosen if item["roll_at"]), key=lambda item: item["roll_at"])
    if not past:
        return None
    last = past[-1]["roll_at"].date()
    years = 1
    while True:
        try:
            guess = last.replace(year=last.year + years)
        except ValueError:  # 29 февраля
            guess = last.replace(year=last.year + years, day=28)
        if guess > now.date():
            return f"{past[-1]['month']}{guess.year}", guess, True
        years += 1


def _add_dates(body, chosen, now=None):
    """Даты зон для конкретного месяца контракта: склейка − дней до экспирации."""
    target = target_roll(chosen, now)
    if target is None:
        return
    code, roll, estimated = target
    today = (now or datetime.now(timezone.utc)).date()
    body["target"] = {"code": code, "roll": roll.isoformat(), "estimated": estimated}
    for zone in body["ranges"]:
        for key in ("buy", "sell"):
            low, high = zone[key]
            zone[key + "_dates"] = [(roll - timedelta(days=high)).isoformat(), (roll - timedelta(days=low)).isoformat()]
        # Первое действие зоны: у лонга покупка, у шорта продажа. Его окно уже прошло?
        first = zone["buy_dates"] if zone["side"] == "лонг" else zone["sell_dates"]
        zone["late"] = first[1] < today.isoformat()


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
    Z, best_z = core.zscore_grid(S, params["hold"], params["years"], params["smooth"])
    body.update({
        "returns": {key: G[key] for key in ("smooth", "ret", "ann", "worst", "win", "mae", "ratio", "n", "side")},
        "move": {key: M[key] for key in ("smooth", "move", "agree", "raw", "n")},
        "direction": {key: D[key] for key in ("smooth", "move", "agree", "raw", "n")},
        "zscore": {key: Z[key] for key in ("smooth", "move", "agree", "raw", "n")},
        "best": {"returns": best, "move": best_move, "direction": best_side, "zscore": best_z},
        "ranges": core.zones(G, D, Z, M, params["share"] / 100),
    })
    if params["month"]:
        _add_dates(body, chosen)
    return core.clean(body)
