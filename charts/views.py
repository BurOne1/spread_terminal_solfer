"""Страница графика и её JSON.

Уровни, режим входа и база цены живут в браузере у каждого пользователя
и приходят параметрами запроса. Стратегия считается на лету и в базу
ничего не пишет. Без параметров берутся уровни, сохранённые у пары.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict

from django.db.models import Count, Max
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, HttpResponseNotModified, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.shortcuts import render

from access.groups import ANALYST, TITLES, group_of, is_viewer
from charts.models import PublishedChannel
from market.models import Candle, Instrument, Pair
from strategy.models import SpreadBar
from strategy.runner import BASES, notional_of, priced, spread_rows

# Ответ /api/spread/ без изменений в барах отдаётся как 304 без пересчёта.
# Раз в столько секунд он всё равно пересчитывается: ставки, фандинг и
# уровни пары в админке меняются без новых баров.
ETAG_TTL = 600


def viewer_pairs(request):
    """Активные пары группы: «Просмотру» — только отмеченные «для просмотра» в админке."""
    pairs = Pair.objects.filter(active=True)
    return pairs.filter(viewer=True) if is_viewer(request) else pairs


def _pair(code, request=None):
    pairs = Pair.objects.filter(active=True) if request is None else viewer_pairs(request)
    if code:
        return pairs.filter(code=code).first() or pairs.order_by("code").first()
    return pairs.order_by("code").first()


def _contracts(pair):
    """Кварталы, чьи окна делят ряд пары. У календаря и секондов это фронты базы."""
    if pair is None:
        return []
    owner = pair
    if pair.quarters_code != pair.code:
        owner = Pair.objects.filter(code=pair.quarters_code).first() or pair
    return list(
        Instrument.objects.filter(pair=owner, kind=Instrument.Kind.FUTURE, roll_at__isnull=False)
        .order_by("roll_at")
        .values_list("code", flat=True)
    )


def _labels(pair, codes):
    """Подписи окон у календаря и секондов: Z2026/H2027 — фронт и второй квартал.

    У BRNYMEX — коды обеих бирж: BRZ6/BZF27.
    """
    if pair is not None and pair.nymex:
        return {
            moex: f"{moex_symbol.split('@')[0]}/{nymex_symbol.split('@')[0]}"
            for moex, moex_symbol, nymex_symbol in _nymex_windows(pair, codes)
        }
    if pair is None or not pair.base:
        return {}
    root = pair.future_root

    def short(code):
        return code[len(root):] if root and code.startswith(root) else code
    return {code: f"{short(code)}/{short(second)}" for code, second in zip(codes, codes[1:])}


def _nymex_windows(pair, codes):
    """(код BR, символ BR, символ BZ) окон из codes. Ноги одного окна — с одним roll_at."""
    legs = Instrument.objects.filter(pair=pair, roll_at__isnull=False)
    foreign = {
        roll: symbol for roll, symbol in
        legs.filter(kind=Instrument.Kind.FOREIGN).values_list("roll_at", "finam_symbol")
    }
    return [
        (code, symbol, foreign[roll])
        for code, symbol, roll in legs.filter(kind=Instrument.Kind.FUTURE, code__in=codes)
        .values_list("code", "finam_symbol", "roll_at")
        if roll in foreign
    ]


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


def _points(pair, start="", end="", basis="close", rows=None):
    """rows — spread_rows пары, если уже прочитаны."""
    if pair is None:
        return []
    codes = _window(_contracts(pair), start, end)
    allowed = set(codes)
    bars = [bar for bar in (spread_rows(pair) if rows is None else rows) if bar.contract__code in allowed]
    if not bars:
        return []
    since = bars[0].ts
    perp_ohlc = {}
    future_ohlc = {}
    legs = {}
    if pair.calendar:
        legs = _calendar_legs(pair, codes, since)
    elif pair.nymex:
        legs = _nymex_legs(pair, codes, since)
    elif pair.foreign_source != "TRI" and not pair.cross and not pair.listed and not pair.ngnymex:
        scale = pair.future_scale or 1
        perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
        if perp is not None:
            perp_ohlc = _ohlc(perp, since)
        for instrument in Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE, code__in=codes):
            future_ohlc.update(_ohlc(instrument, since, scale))
    points = []
    for bar in bars:
        raw, rate = priced(bar, basis)
        future = future_ohlc.get(bar.ts)
        perp_bar = perp_ohlc.get(bar.ts)
        if legs:
            near, far = legs.get(bar.contract__code, ({}, {}))
            perp_bar, future = near.get(bar.ts), far.get(bar.ts)
        if future and perp_bar:
            opened = future[0] - perp_bar[0]
            closed = future[3] - perp_bar[3]
            high = max(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
            low = min(future[1] - perp_bar[2], future[2] - perp_bar[1], opened, closed)
        else:
            closed = float(bar.raw_spread)
            opened = closed if bar.raw_open is None else float(bar.raw_open)
            high, low = max(opened, closed), min(opened, closed)
        points.append({
            "t": bar.ts.isoformat(),
            "contract": bar.contract__code,
            "raw": raw,
            "rate": rate,
            "days": bar.days_left,
            "price": float(bar.spot or bar.perp_close or 0),
            "candle": {"o": opened, "h": high, "l": low, "c": closed},
        })
    return points


def _calendar_legs(pair, codes, since):
    """Фронт → (свечи фронта, свечи второго квартала) в единицах базы."""
    base = Pair.objects.filter(code=pair.base).first()
    if base is None:
        return {}
    scale = base.future_scale or 1
    quarters = list(
        Instrument.objects.filter(pair=base, kind=Instrument.Kind.FUTURE, roll_at__isnull=False).order_by("roll_at")
    )
    wanted = set(codes)
    return {
        front.code: (_ohlc(front, since, scale), _ohlc(second, since, scale))
        for front, second in zip(quarters, quarters[1:])
        if front.code in wanted
    }


def _nymex_legs(pair, codes, since):
    """BR → (свечи BZ, свечи BR): свеча спреда BR − BZ, как у календаря."""
    legs = Instrument.objects.filter(pair=pair, roll_at__isnull=False)
    foreign = {leg.roll_at: leg for leg in legs.filter(kind=Instrument.Kind.FOREIGN)}
    return {
        moex.code: (_ohlc(foreign[moex.roll_at], since), _ohlc(moex, since))
        for moex in legs.filter(kind=Instrument.Kind.FUTURE, code__in=codes)
        if moex.roll_at in foreign
    }


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
        "tp_all": request.GET.get("tp") == "all",
    }


def _strategy(pair, request, codes, rows=None):
    """PNL, число сделок и открытая позиция для выбранного окна контрактов."""
    from strategy.runner import pass_pair
    options = _settings(request)
    points, trades, opened = pass_pair(pair, levels=_user_levels(request, pair), rows=rows, **options)
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
        "absolute": pair.absolute_levels,
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
    """Цена, от которой уровни — проценты.

    У треугольника это вечный металл, руб/грамм. У календаря — спот
    последнего бара: спот базы или, у GD и SV, фронт. У BRNYMEX — цена BZ,
    у кросс-пар — Si или Eu. У календаря NG уровни абсолютные: 1.
    """
    if pair.absolute_levels:
        return 1.0
    if pair.calendar or pair.zero_fair:
        last = SpreadBar.objects.filter(pair=pair).order_by("-ts").values_list("spot", flat=True).first()
        return None if last is None else float(last)
    if pair.foreign_source == "TRI":
        from strategy.rebuild import TRI_PERP, TRI_SPOT
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.tri_code, "")).first()
        if metal is None:
            return None
        third = metal.spot_code if pair.tri_code in TRI_SPOT else metal.perp_code
        perp = Instrument.objects.filter(pair=metal, code=third).first()
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
    if pair is not None and (pair.calendar or pair.zero_fair):
        return []
    if pair is not None and pair.foreign_source == "TRI":
        from strategy.rebuild import TRI_SPOT
        if pair.tri_code == "TRIGL" or pair.tri_code in TRI_SPOT:
            return []
        from strategy.rebuild import TRI_PERP
        metal = Pair.objects.filter(code=TRI_PERP.get(pair.tri_code, "")).first()
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
    if pair.calendar or pair.zero_fair:
        return 1
    if pair.foreign_source == "TRI":
        return 100 if pair.tri_code == "TRISV" else 1
    if pair.code == "SILVER":
        return 100
    if pair.code == "GOLD":
        return 1
    return pair.lot or 1000


def _current(pair, contracts):
    """Текущий контракт: первый, чья склейка ещё впереди; все склеились — последний."""
    if not contracts:
        return ""
    owner = pair
    if pair.quarters_code != pair.code:
        owner = Pair.objects.filter(code=pair.quarters_code).first() or pair
    ahead = (
        Instrument.objects.filter(pair=owner, kind=Instrument.Kind.FUTURE, code__in=contracts, roll_at__gt=timezone.now())
        .order_by("roll_at").values_list("code", flat=True).first()
    )
    return ahead or contracts[-1]


def _range(request, contracts, pair=None):
    """Окно контрактов из адреса. Не задано — только текущий контракт, а не вся история.

    Задан один край — второй тоже текущий, но окно не переворачивается.
    """
    if not contracts:
        return "", ""
    start = request.GET.get("from", "")
    end = request.GET.get("to", "")
    current = _current(pair, contracts) if pair is not None else contracts[-1]
    if start not in contracts:
        start = current if end not in contracts else min(current, end, key=contracts.index)
    if end not in contracts:
        end = max(current, start, key=contracts.index)
    return start, end
    current = _current(pair, contracts) if pair is not None else (contracts[-1] if contracts else "")
    if start not in contracts:
        start = current if end not in contracts or contracts.index(end) >= contracts.index(current) else end
    if end not in contracts:
        end = current if contracts.index(start) <= contracts.index(current) else start
    return start, end


LAST_PAIR_COOKIE = "spread_pair"


def last_pair_code(request):
    """Пара из адреса, иначе последняя выбранная (cookie ставят страницы спредов и аналитики)."""
    code = request.GET.get("pair", "") or request.COOKIES.get(LAST_PAIR_COOKIE, "")
    return code if viewer_pairs(request).filter(code=code).exists() else ""


@ensure_csrf_cookie
def home(request):
    """Каркас страницы. Данные с настройками браузера приходят первым же /api/spread/.

    Зрителю — только его пары, без рисования, стратегий и индикаторов (viewer в шаблоне).
    """
    pairs = list(viewer_pairs(request).order_by("code"))
    pair = _pair(last_pair_code(request), request)
    contracts = _contracts(pair)
    start, end = _range(request, contracts, pair)
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
        "title": _title(pair),
        "subtitle": _subtitle(pair),
        "viewer": is_viewer(request),
        "group_title": TITLES.get(group_of(request), ""),
    })


def _title(pair):
    if pair is None:
        return ""
    if pair.foreign_source == "TRI" or pair.calendar or pair.zero_fair:
        return pair.title
    return f"{pair.future_root} − {pair.perp_code}"


def _subtitle(pair):
    if pair is None:
        return ""
    if pair.foreign_source == "TRI":
        return f"руб/грамм, {_rate_label(pair)}"
    if pair.calendar:
        return f"{_rate_label(pair)}, {pair.quote}"
    if pair.nymex:
        return "месячные, только общие часы бирж, справедливый 0, $/барр."
    if pair.cross:
        from market.cross import CROSS
        return f"{CROSS[pair.code]['note']}, третья нога CME, справедливый 0"
    if pair.listed:
        return "календарь биржи одним инструментом, $/MMBtu, уровни в $/MMBtu"
    if pair.ngnymex:
        return "месячные, NYMEX месяцем позже, только общие часы бирж, справедливый 0, $/MMBtu"
    if pair.crypto:
        return f"месячный {pair.future_root} − вечный {pair.perp_code}, справедливый 0, фандинг вечного, $"
    rate = "только RUONIA" if pair.foreign_source == "NONE" else _rate_label(pair)
    return f"Спот {pair.spot_code}, ставка {rate}, {pair.quote}"


def watchlist_json(request):
    """Последний бар каждой активной пары и изменение spread_rate за сутки."""
    from datetime import timedelta
    basis = _settings(request)["basis"]
    rows = []
    for pair in viewer_pairs(request).order_by("code"):
        bars = SpreadBar.objects.filter(pair=pair).select_related("contract").order_by("-ts")
        last = bars.first()
        labels = _labels(pair, _contracts(pair)) if last is not None and (pair.base or pair.nymex) else {}
        # second — пары на вторых кварталах: календари и треугольники с base.
        item = {
            "code": pair.code, "title": _title(pair), "name": pair.title, "venue": pair.venue,
            "group": "second" if pair.base else "front",
        }
        if last is not None:
            raw, rate = priced(last, basis)
            before = bars.filter(ts__lte=last.ts - timedelta(days=1)).first()
            old_rate = None if before is None else priced(before, basis)[1]
            item.update({
                "t": last.ts.isoformat(),
                "contract": labels.get(last.contract.code, last.contract.code),
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
        seconds = " на секондах" if pair.base else ""
        if pair.tri_code == "TRIGL":
            return f"треугольник{seconds}, ноги чистятся отдельно"
        if pair.code in ("TRIGDS", "TRISVS"):
            spot = "GLDRUB_TOM" if pair.code == "TRIGDS" else "SLVRUB_TOM"
            return f"треугольник на споте {spot}, RUONIA + ставка ФРС, без фандинга"
        return f"треугольник{seconds}, RUONIA и фандинг вечного"
    if pair.calendar:
        base = Pair.objects.filter(code=pair.base).first()
        if base is None or base.foreign_source == "TRI":
            return "секонд − фронт, ставка ФРС"
        foreign = "" if base.foreign_source == "NONE" else f" − {base.foreign_source}"
        return f"секонд − фронт, RUONIA{foreign}"
    if pair.foreign_source == "EWMA14":
        return "EWMA14 фандинга"
    if pair.nymex:
        return "BR MOEX − BZ NYMEX"
    if pair.cross or pair.listed or pair.ngnymex:
        return pair.title
    if pair.crypto:
        return "справедливый 0"
    return pair.foreign_source


def _spread_etag(request):
    """Версия ответа: пара, настройки из запроса, бары пары и фандинг.

    Пересчёт хвоста удаляет и заново вставляет бары, поэтому любое
    изменение ряда двигает число или последний id.
    """
    from rates.models import FundingDay
    pair = _pair(request.GET.get("pair", ""), request)
    if pair is None:
        return None
    bars = SpreadBar.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("id"))
    funding = FundingDay.objects.aggregate(n=Count("id"), last=Max("date"))
    key = "|".join(str(item) for item in (
        pair.code, request.GET.urlencode(), bars["n"], bars["last"], funding["n"], funding["last"],
        int(time.time() // ETAG_TTL), is_viewer(request), published_version(pair),
    ))
    request.spread_version = hashlib.md5(key.encode()).hexdigest()
    return request.spread_version


# Готовые ответы /api/spread/: страница опрашивает раз в 5 с, а run_live двигает бары
# почти каждый опрос. Без кэша каждый опрос читал всю историю пары и гонял стратегию
# по всем барам. Теперь те же настройки пересчитываются не чаще SPREAD_MIN_AGE секунд,
# между пересчётами браузер получает 304. Кэш — байты JSON, лимит по объёму на процесс.
SPREAD_MIN_AGE = getattr(settings, "SPREAD_MIN_AGE", 30)
SPREAD_CACHE_BYTES = 32 * 1024 * 1024
_spread_cache = OrderedDict()
_spread_lock = threading.Lock()


def spread_json(request):
    code = request.GET.get("pair", "")
    pair = _pair(code, request)
    if code and (pair is None or pair.code != code):
        # Не подменять молча первой парой: пользователь увидел бы чужой спред.
        return JsonResponse({"error": f"пары {code} нет среди доступных"}, status=404)
    version = _spread_etag(request) or ""
    key = ("" if pair is None else pair.code, request.GET.urlencode(), is_viewer(request))
    now = time.monotonic()
    with _spread_lock:
        entry = _spread_cache.get(key)
        if entry is not None:
            _spread_cache.move_to_end(key)
    if entry is None or (entry[0] != version and now - entry[1] >= SPREAD_MIN_AGE):
        data = json.dumps(_spread_body(request, pair, version), cls=DjangoJSONEncoder,
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry = (version, now, data)
        with _spread_lock:
            _spread_cache[key] = entry
            _spread_cache.move_to_end(key)
            while len(_spread_cache) > 1 and sum(len(item[2]) for item in _spread_cache.values()) > SPREAD_CACHE_BYTES:
                _spread_cache.popitem(last=False)
    etag = f'"{entry[0]}"'
    if etag in request.headers.get("If-None-Match", ""):
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(entry[2], content_type="application/json")
    response["ETag"] = etag
    # Браузер переспрашивает с If-None-Match и без изменений получает 304.
    response["Cache-Control"] = "no-cache"
    return response


def _spread_body(request, pair, version):
    contracts = _contracts(pair)
    start, end = _range(request, contracts, pair)
    options = _settings(request)
    rows = None if pair is None else spread_rows(pair)
    points = _points(pair, start, end, options["basis"], rows)
    pnl, trades, opened = [], 0, None
    # strategy=off — стратегию считает браузер (канал) или она выключена. У зрителя её нет вовсе.
    if pair is not None and request.GET.get("strategy") != "off" and not is_viewer(request):
        pnl, trades, opened = _strategy(pair, request, _window(contracts, start, end), rows)
    return {
        "version": version,
        "pair": "" if pair is None else pair.code,
        "title": _title(pair),
        "subtitle": _subtitle(pair),
        "venue": "" if pair is None else pair.venue,
        "spot": "" if pair is None else pair.spot_code,
        "rate": _rate_label(pair),
        "quote": "" if pair is None else pair.quote,
        "root": "" if pair is None else pair.future_root,
        "contracts": contracts,
        "labels": _labels(pair, contracts),
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
        "notional": None if pair is None else notional_of(pair),
        "published": None if pair is None else published_version(pair),
        "strategy_funding": _strategy_funding(pair),
    }


def _strategy_funding(pair):
    """Фандинг в том виде, в каком его берёт движок: для стратегии канала в браузере.

    daily — [дата, деньги на контракт за день], interval — [время, ставка за 8 ч].
    """
    if pair is None:
        return {"mode": "daily", "rows": []}
    from strategy.runner import funding_of
    rows, mode = funding_of(pair)
    return {"mode": mode, "rows": [[stamp.isoformat(), value] for stamp, value in rows]}


def published_version(pair):
    """Версия опубликованных каналов пары: меняется при публикации и снятии."""
    rows = PublishedChannel.objects.filter(pair=pair).aggregate(n=Count("id"), last=Max("published_at"))
    return f"{rows['n']}-{rows['last'].timestamp() if rows['last'] else 0}"


def _channel_json(item):
    return {
        "id": item.drawing_id, "data": item.data, "lines": item.lines,
        "published_at": item.published_at.isoformat(),
    }


@require_http_methods(["GET", "POST", "DELETE"])
def published_json(request):
    """Каналы для зрителей.

    GET ?pair= — опубликованные каналы пары (обеим группам).
    POST {pair, drawing, lines} — опубликовать или обновить рисунок (аналитики).
    DELETE {pair, id} — снять с публикации (аналитики).
    """
    if request.method == "GET":
        pair = _pair(request.GET.get("pair", ""), request)
        if pair is None or pair.code != request.GET.get("pair", ""):
            return JsonResponse({"error": "нет такой пары"}, status=404)
        items = PublishedChannel.objects.filter(pair=pair).order_by("published_at")
        return JsonResponse({"pair": pair.code, "version": published_version(pair), "channels": [_channel_json(item) for item in items]})
    if group_of(request) != ANALYST:
        return JsonResponse({"error": "публикует только отдел трейдинга"}, status=403)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "нужен JSON"}, status=400)
    pair = Pair.objects.filter(active=True, code=body.get("pair", "")).first()
    if pair is None:
        return JsonResponse({"error": "нет такой пары"}, status=404)
    if request.method == "DELETE":
        removed, _ = PublishedChannel.objects.filter(pair=pair, drawing_id=str(body.get("id", ""))).delete()
        return JsonResponse({"removed": removed, "version": published_version(pair)})
    drawing = body.get("drawing") or {}
    points = drawing.get("points") or []
    if drawing.get("kind") != "parallel-channel" or len(points) < 3 or not drawing.get("id"):
        return JsonResponse({"error": "публикуются только параллельные каналы"}, status=400)
    if not all(isinstance(point, dict) and isinstance(point.get("time"), (int, float)) and isinstance(point.get("price"), (int, float)) for point in points[:3]):
        return JsonResponse({"error": "у канала нет точек время/цена"}, status=400)
    lines = "raw" if body.get("lines") == "raw" else "rate"
    item, _created = PublishedChannel.objects.update_or_create(
        pair=pair, drawing_id=str(drawing["id"])[:64],
        # Переопубликовали — оповещения начинают с чистого листа.
        defaults={"data": drawing, "lines": lines, "zone": "", "event": "", "event_at": None},
    )
    return JsonResponse({"channel": _channel_json(item), "version": published_version(pair)})
