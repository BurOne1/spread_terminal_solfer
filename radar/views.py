"""Страница радара и её JSON. Всё читается из базы: run_radar — отдельный процесс.

Нет run_radar, ключа Finam или AlgoPack — страница всё равно рисуется,
а состояние источников показывает, что не подключено и почему.
"""

from datetime import datetime, timedelta, timezone

from django.db.models import OuterRef, Subquery
from django.http import JsonResponse
from django.shortcuts import render

from market.models import Candle, Instrument
from radar.models import Anomaly, BookState, MinuteStat, SourceStatus, Trade

# Отметка run_radar старше этого — процесс считается остановленным.
STALE = timedelta(seconds=30)
SOURCES = (("finam", "Finam"), ("algopack", "AlgoPack"), ("radar", "Радар"))
TAPE = 80
FEED = 150
CHART_DAYS = 2
MSK = timedelta(hours=3)


def page(request):
    return render(request, "radar/radar.html", {"symbol": request.GET.get("symbol", "")})


def _moscow(ts):
    return (ts + MSK).strftime("%d.%m %H:%M:%S")


def _statuses(now):
    rows = {row.name: row for row in SourceStatus.objects.all()}
    radar = rows.get("radar")
    alive = radar is not None and now - radar.updated_at < STALE
    found = []
    for name, label in SOURCES:
        row = rows.get(name)
        if name == "radar":
            state = "ok" if alive else "off"
            if alive:
                message = row.message
            elif row is None:
                message = "run_radar ещё не запускался: python manage.py run_radar"
            else:
                message = f"run_radar не запущен, последняя отметка {_moscow(row.updated_at)} МСК"
        elif row is None:
            state, message = "off", "нет данных: run_radar ещё не запускался"
        elif not alive:
            state, message = "off", f"run_radar не запущен; было: {row.get_state_display()}, {row.message}"
        else:
            state, message = row.state, row.message
        found.append({"name": name, "label": label, "state": state, "message": message})
    return found, alive


def _number(text, default=0.0):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


# Виджетов одного вида на странице не больше: защита от слишком длинного запроса.
MAX_KEYS = 12


def _keys(request, name):
    """Список ключей виджетов из параметра через запятую, без повторов."""
    return list(dict.fromkeys(key for key in request.GET.get(name, "").split(",") if key))[:MAX_KEYS]


def _tape(symbol, min_size):
    rows = (
        Trade.objects.filter(symbol=symbol, size__gte=min_size)
        .order_by("-ts", "-id").values_list("ts", "price", "size", "side")[:TAPE]
    )
    return [{"t": ts.isoformat(), "p": float(price), "s": float(size), "d": side} for ts, price, size, side in rows]


def _feed(now, kind, symbol, titles):
    rows = Anomaly.objects.filter(ts__gte=now - timedelta(days=1))
    if kind:
        rows = rows.filter(kind=kind)
    if symbol:
        rows = rows.filter(symbol=symbol)
    return [
        {
            "id": item.id, "t": item.ts.isoformat(), "symbol": item.symbol,
            "title": titles.get(item.symbol, item.symbol), "kind": item.kind,
            "kind_label": item.get_kind_display(), "side": item.side,
            "price": None if item.price is None else float(item.price),
            "size": None if item.size is None else float(item.size),
            "score": item.score, "text": item.text,
        }
        for item in rows.order_by("-ts", "-id")[:FEED]
    ]


def state_json(request):
    """Источники, список символов и данные виджетов. Опрос раз в секунду.

    books=SiZ6@RTSX,CRZ6@RTSX — стаканы; tapes=SiZ6@RTSX|10,... — ленты с
    фильтром размера; feeds=sweep|SiZ6@RTSX,|... — ленты аномалий: тип и
    символ, пустые — все. Ответ — словари по тем же ключам.
    """
    now = datetime.now(timezone.utc)
    statuses, alive = _statuses(now)
    books = list(BookState.objects.all())
    known = {book.symbol: book for book in books}
    titles = {book.symbol: book.title for book in books}
    tapes = {}
    for key in _keys(request, "tapes"):
        symbol, _bar, min_size = key.partition("|")
        tapes[key] = _tape(symbol, _number(min_size)) if symbol else []
    feeds = {}
    for key in _keys(request, "feeds"):
        kind, _bar, symbol = key.partition("|")
        feeds[key] = _feed(now, kind, symbol, titles)
    return JsonResponse({
        "now": now.isoformat(),
        "alive": alive,
        "statuses": statuses,
        "symbols": [
            {"symbol": item.symbol, "title": item.title, "group": item.group, "summary": item.summary}
            for item in books
        ],
        "books": {
            symbol: None if symbol not in known else {
                "asks": known[symbol].asks, "bids": known[symbol].bids,
                "ts": None if known[symbol].ts is None else known[symbol].ts.isoformat(),
            }
            for symbol in _keys(request, "books")
        },
        "tapes": tapes,
        "feeds": feeds,
    })


def chart_json(request):
    """Минутные свечи символа, накопленная за день дельта, открытый интерес и аномалии."""
    symbol = request.GET.get("symbol", "")
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=CHART_DAYS)
    rows = list(
        MinuteStat.objects.filter(symbol=symbol, ts__gte=since).order_by("ts")
        .values_list("ts", "open", "high", "low", "close", "buy", "sell", "volume", "oi")
    )
    candles, delta, oi = [], [], []
    running, day = 0.0, None
    for ts, opened, high, low, closed, buy, sell, volume, interest in rows:
        stamp = ts.isoformat()
        moscow_day = (ts + MSK).date()
        if moscow_day != day:
            running, day = 0.0, moscow_day
        running += float(buy) - float(sell)
        candles.append({"t": stamp, "o": float(opened), "h": float(high), "l": float(low), "c": float(closed), "v": float(volume)})
        delta.append({"t": stamp, "v": running})
        if interest is not None:
            oi.append({"t": stamp, "v": float(interest)})
    markers = [
        {
            "t": ts.isoformat(), "kind": kind, "side": side,
            "price": None if price is None else float(price), "text": text,
        }
        for ts, kind, side, price, text in
        Anomaly.objects.filter(symbol=symbol, ts__gte=since).order_by("ts")
        .values_list("ts", "kind", "side", "price", "text")[:2000]
    ]
    return JsonResponse({"symbol": symbol, "candles": candles, "delta": delta, "oi": oi, "markers": markers})


def instruments_json(request):
    """Инструменты базы, которых нет в радаре: последняя часовая свеча. Опрос раз в минуту."""
    watched = set(BookState.objects.values_list("symbol", flat=True))
    latest = Candle.objects.filter(instrument=OuterRef("pk")).order_by("-ts")
    rows = (
        Instrument.objects.exclude(finam_symbol="")
        .annotate(last_ts=Subquery(latest.values("ts")[:1]), last_close=Subquery(latest.values("close")[:1]))
        .filter(last_ts__gte=datetime.now(timezone.utc) - timedelta(days=10))
        .select_related("pair").order_by("pair__code", "code")
    )
    return JsonResponse({"instruments": [
        {
            "code": item.code, "symbol": item.finam_symbol, "pair": item.pair.code if item.pair else "",
            "close": float(item.last_close), "t": item.last_ts.isoformat(),
        }
        for item in rows if item.finam_symbol not in watched
    ]})
