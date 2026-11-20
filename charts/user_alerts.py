"""Личные алерты по рисункам: одно срабатывание, отдельный чат, сразу после пересчёта спреда.

Проверка идёт в run_live после каждого 5-секундного пересчёта хвостов (_flush_now):
котировки сокета → свечи ног → спред. Значение — последний бар пары (1ч),
его закрытие — последние сделки ног. Сравнивается с прошлым значением алерта:
переход через линию между двумя проверками ловится, даже если спред перепрыгнул её.

Рисунки:
- горизонтальная линия — уровень, любое пересечение;
- трендовая линия и луч — наклонная по номерам баров, как каналы (charts/alerts.py);
  правее своей второй точки линия для алерта продолжается; «сейчас» — с долей бара,
  линия движется плавно, а не скачком раз в 5 минут;
- параллельный канал — касание верхней или нижней границы и (или) переход середины,
  на выбор (Alert.target);
- прямоугольник — вход в зону цены в интервале его времени;
- уровень в % спота (level-pct, задаётся с клавиатуры) — доля спота последнего бара, как уровни
  стратегии: линия идёт за спотом. Пересечение в любую сторону.

Канал, трендовая линия и луч могут повторяться (Alert.repeat): после срабатывания алерт не
выключается, следующее сообщение — не раньше паузы pause_minutes; события в паузе не копятся.

Первая проверка только запоминает сторону: старое пересечение задним числом не приходит.
Сработавший алерт больше не проверяется (status = fired), его включают заново на странице.

Ноги: в сообщении — возраст последней сделки каждой ноги. Алерт ждёт, пока по всем
известным ногам в этот же день (по Москве) были сделки: в начале дня одна нога
не берётся с прошлого дня. И пока в стакане каждой ноги есть и покупатели, и продавцы
(стакан радара run_radar; нет данных радара — не мешает, в сообщении так и написано).
Ноги, время сделок (лента радара и сокет run_live) и стаканы даёт market.live.pair_legs.
"""

import html
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.utils import timezone as dj_timezone

from bisect import bisect_right

from charts.alerts import bounds, fill_times, index_of, send, trading_hour
from charts.models import Alert
from strategy.models import SpreadBar

KINDS = ("horizontal-line", "trend-line", "ray", "parallel-channel", "rectangle", "level-pct")
# Эти могут повторяться (Alert.repeat): после срабатывания ждут паузу и работают дальше.
REPEATABLE = ("trend-line", "ray", "parallel-channel")
TITLES = {
    "horizontal-line": "горизонтальная линия",
    "trend-line": "трендовая линия",
    "ray": "луч",
    "parallel-channel": "канал",
    "rectangle": "прямоугольник",
    "level-pct": "уровень в % спота",
}
MSK = timezone(timedelta(hours=3))
# За столько до склейки контракта пары ждущие алерты выключаются: после склейки спред
# переходит на новый контракт, и линии, нарисованные по старому, ложно срабатывают.
ROLL_OFF = timedelta(hours=4)
# Нога спреда для алерта: время последней сделки, стакан (True — есть покупатели и продавцы,
# False — одна сторона пуста, None — не знаем), approx — время только по 5-минуткам базы.
# bookless — у ноги стакана не бывает (спот сервиса котировок USDCNY, EURUSD).
Leg = namedtuple("Leg", "name ts book approx bookless", defaults=(None, False, False))


def _points(alert):
    return [point for point in (alert.data or {}).get("points") or []
            if isinstance(point, dict) and isinstance(point.get("price"), (int, float))]


def _times(pair, start, cache):
    """Номера баров для наклонных рисунков: 5-минутки с запасом до первой точки (или часы)."""
    from charts import frames as tf
    key = (pair.id, start)
    if key not in cache:
        since = datetime.fromtimestamp(start, timezone.utc) - timedelta(days=3)
        frame = "M5"
        rows = tf.rows(pair, frame, since=since)
        if not rows:
            frame = "H1"
            rows = tf.rows(pair, frame, since=since)
        minutes = tf.MINUTES[frame]
        cache[key] = (fill_times([int(row.ts.timestamp()) for row in rows], pair.venue, minutes), minutes)
    return cache[key]


def position(times, stamp, venue, minutes):
    """Номер бара для момента stamp с дробной частью: линия идёт плавно, а не ступеньками по бару.

    Внутри ряда — между соседними барами пропорционально времени; после последнего бара —
    целые торговые слоты (как пустые слоты графика) плюс доля текущего слота, если он торговый.
    """
    if not times:
        return None
    if stamp <= times[-1]:
        index = bisect_right(times, stamp) - 1
        if index < 0:
            return 0.0
        if index == len(times) - 1:
            return float(index)
        return index + (stamp - times[index]) / (times[index + 1] - times[index])
    step = minutes * 60
    index, moment, trading = len(times) - 1, times[-1], True
    while moment + step <= stamp:
        moment += step
        trading = trading_hour(moment, venue, minutes)
        if trading:
            index += 1
    return index + ((stamp - moment) / step if trading else 0.0)


def _slope_line(alert, stamp, cache):
    """Цена наклонной линии в момент stamp или None, если момент вне линии."""
    points = _points(alert)
    if len(points) < 2 or not all(isinstance(point.get("time"), (int, float)) for point in points[:2]):
        return None
    p0, p1 = sorted(points[:2], key=lambda point: point["time"])
    style = (alert.data or {}).get("style") or {}
    if alert.kind == "ray":
        first = points[0]
        right = points[1]["time"] >= first["time"]
        if (right and stamp < first["time"]) or (not right and stamp > first["time"]):
            return None
    elif stamp < p0["time"] and not style.get("extendLeft"):
        # Правее второй точки трендовая линия для алерта продолжается: иначе после
        # конца отрезка алерт молча перестаёт проверяться.
        return None
    times, minutes = _times(alert.pair, int(p0["time"]), cache)
    venue = alert.pair.venue
    i0, i1 = (index_of(times, value, venue, minutes) for value in (p0["time"], p1["time"]))
    index = position(times, stamp, venue, minutes)
    if None in (i0, i1, index):
        return None
    if i1 == i0:
        return p0["price"]
    return p0["price"] + (p1["price"] - p0["price"]) * (index - i0) / (i1 - i0)


def _side(value, level):
    if value > level:
        return "above"
    if value < level:
        return "below"
    return "on"


def state(alert, value, stamp, cache, spot=None):
    """(сторона, описание рисунка в момент stamp). Сторона пустая — рисунок сейчас не действует.

    spot — цена, от которой считаются уровни в процентах (spot последнего бара пары).
    """
    kind = alert.kind
    if kind == "level-pct":
        share = (alert.data or {}).get("share")
        if not isinstance(share, (int, float)) or not spot:
            return "", ""
        level = share * spot
        return _side(value, level), f"уровень {share * 100:.3f}%"
    points = _points(alert)
    if not points:
        return "", ""
    if kind == "horizontal-line":
        level = points[0]["price"]
        return _side(value, level), f"уровень {_number(level)}"
    if kind in ("trend-line", "ray"):
        level = _slope_line(alert, stamp, cache)
        if level is None:
            return "", ""
        return _side(value, level), f"линия {_number(level)}"
    if kind == "parallel-channel":
        if len(points) < 3:
            return "", ""
        start = int(min(point["time"] for point in points[:3]))
        times, minutes = _times(alert.pair, start, cache)
        found = bounds(points, times, alert.pair.venue, position(times, stamp, alert.pair.venue, minutes) or 0, minutes)
        if found is None:
            return "", ""
        lower, middle, upper = found
        return channel_zone(value, lower, middle, upper), f"канал {_number(lower)} · середина {_number(middle)} · {_number(upper)}"
    if kind == "rectangle":
        if len(points) < 2:
            return "", ""
        start, end = sorted(point["time"] for point in points[:2])
        low, high = sorted(point["price"] for point in points[:2])
        if stamp < start or stamp > end:
            return "", ""
        side = "in" if low <= value <= high else ("above" if value > high else "below")
        return side, f"зона {_number(low)} … {_number(high)}"
    return "", ""


def channel_zone(value, lower, middle, upper):
    """Зона спреда в канале. В отличие от каналов «Просмотра» (charts/alerts.zone_of) середина —
    своя зона mid: достиг её — уже событие, как у границ (above / below — на границе и за ней)."""
    if value >= upper:
        return "above"
    if value <= lower:
        return "below"
    if value == middle:
        return "mid"
    return "upper" if value > middle else "lower"


def event_of(kind, before, after, target=Alert.BOTH):
    """Текст события или пусто. before пусто — первая проверка, только запомнить."""
    if not before or not after or before == after:
        return ""
    if kind == "parallel-channel":
        if target != Alert.MIDDLE and after == "above":
            return "коснулся верхней границы"
        if target != Alert.MIDDLE and after == "below":
            return "коснулся нижней границы"
        # Середина — по достижению: снизу дошёл до неё (mid) или прошёл выше, и наоборот.
        low, high = {"below", "lower"}, {"upper", "above"}
        if target != Alert.EDGES and before in low and (after == "mid" or after in high):
            return "достиг середины снизу" if after == "mid" else "пересёк середину вверх"
        if target != Alert.EDGES and before in high and (after == "mid" or after in low):
            return "достиг середины сверху" if after == "mid" else "пересёк середину вниз"
        return ""
    if kind == "rectangle":
        if after == "in":
            return "вошёл в зону"
        if {before, after} == {"above", "below"}:
            return "прошёл через зону"
        return ""
    if after == "on" or before == "on":
        return "коснулся" if after == "on" else ""
    return "пересёк вверх" if after == "above" else "пересёк вниз"


def _number(value):
    return f"{value:,.3f}".replace(",", " ")


def _age(ts, now):
    seconds = max(0, int((now - ts).total_seconds()))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def same_day(legs, now):
    """У всех ног последняя сделка — сегодня по Москве. Нога без времени — не проверена."""
    today = now.astimezone(MSK).date()
    return all(leg.ts is not None and leg.ts.astimezone(MSK).date() == today for leg in legs)


# Свежесть ног: алерт ждёт, пока последняя сделка каждой ноги не старше этого.
FRESH = timedelta(minutes=2)
# Редкие ноги — CME (6E, CNH) и спот сервиса котировок (USDCNY, EURUSD): им дольше.
FRESH_SLOW = timedelta(minutes=4)
SLOW_PREFIXES = ("6E", "CNH")
SLOW_NAMES = ("USDCNY", "EURUSD")


def leg_limit(leg):
    return FRESH_SLOW if leg.name.startswith(SLOW_PREFIXES) or leg.name in SLOW_NAMES else FRESH


def fresh(legs, now):
    """Каждая нога торговалась не раньше своего лимита. Время только по 5-минутке (approx) —
    это начало 5-минутки, сделка могла быть до 5 минут позже: считается её концом."""
    for leg in legs:
        if leg.ts is None:
            return False
        last = leg.ts + timedelta(minutes=5) if leg.approx else leg.ts
        if now - last > leg_limit(leg):
            return False
    return True


def books_ok(legs):
    """В стакане каждой ноги кто-то есть. Нога, про стакан которой не знаем, не мешает."""
    return all(leg.book is not False for leg in legs)


def _leg_text(leg, now):
    if leg.ts is None:
        return f"{leg.name} — нет сделок"
    if leg.approx:
        return f"{leg.name} — сделка не позже {_age(leg.ts, now)} назад (по 5-минуткам)"
    return f"{leg.name} — {_age(leg.ts, now)} назад"


def db_legs(pair, now):
    """Ноги без run_live: ноги последнего бара по символам подписки и их последние 5-минутки."""
    try:
        from market.live import pair_legs
        return pair_legs(pair, now, memory=False)
    except Exception:
        return []


def message(alert, event, value, where, stamp, legs, now):
    """Текст сообщения в разметке HTML Телеграма, коротко: заголовок жирным, событие, время.

    ⚠️ TRIGD · GD + SI − GLDRUBF · уровень в % спота 🌋      ← жирным
    Очищенный спред пересёк вниз: уровень -0.920%

    20.11 09:53:56 UTC

    Менять формат — здесь. Всё, что не тег, проходит через html.escape (e):
    «<», «>» и «&» в названиях иначе ломают разметку. Для страницы — plain(text).
    value — значение спреда при срабатывании (в базе — fired_value, в списке алертов).
    legs — ноги с временем последней сделки (_leg_text): проверяются, в текст не идут.
    Лоты — вкладка «Лоты» на странице (charts/lots.py), стакан проверяется, но не пишется.
    """
    e = html.escape
    pair = alert.pair
    line = "Сырой спред" if alert.lines == "raw" else "Очищенный спред"
    return (
        f"<b>⚠️ {e(pair.code)} · {e(pair.title)} · {e(TITLES.get(alert.kind, alert.kind))} 🌋</b>\n"
        f"{line} {e(event)}: {e(where)}\n"
        f"\n{stamp:%d.%m %H:%M:%S} UTC"
    )


def plain(text):
    """Сообщение без разметки: для всплывающего окна на странице и списка алертов."""
    return html.unescape(re.sub(r"</?[a-z]+>", "", text))


def number_of(alert):
    """Номер алерта на паре по порядку создания — как «#2» в списке на странице."""
    ids = list(Alert.objects.filter(pair_id=alert.pair_id).order_by("created_at", "id").values_list("id", flat=True))
    return ids.index(alert.pk) + 1 if alert.pk in ids else 0


def roll_message(pair, alerts, contract, roll, now):
    """Сообщение о снятии алертов перед склейкой. Формат — как у message(), менять здесь."""
    e = html.escape
    left = roll - now
    hours, minutes = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
    names = ", ".join(f"#{number_of(alert)} {TITLES.get(alert.kind, alert.kind)}" for alert in alerts)
    return (
        f"<b>⏸ {e(pair.code)} · {e(pair.title)} · алерты сняты</b>\n"
        f"Причина: скорая экспирация — склейка {e(contract)} через {hours} ч {minutes:02d} мин "
        f"({roll:%d.%m %H:%M} UTC).\n"
        f"Сняты: {e(names)}. Включить снова — ▶ в списке алертов.\n"
        f"{now:%d.%m %H:%M:%S} UTC"
    )


def roll_off(pair, items, bar, now, log=None, chat=""):
    """Снять ждущие алерты пары за ROLL_OFF до склейки её контракта. Вернуть снятые."""
    roll = getattr(bar.contract, "roll_at", None)
    if roll is None or not now < roll <= now + ROLL_OFF:
        return []
    due = [alert for alert in items if alert.roll_off_at != roll]
    if not due:
        return []
    Alert.objects.filter(pk__in=[alert.pk for alert in due]).update(status=Alert.PAUSED, roll_off_at=roll)
    send(roll_message(pair, due, bar.contract.code, roll, now), log, chat=chat, missing="TELEGRAM_ALERT_CHAT_ID", html=True)
    return due


def check_alerts(codes=None, log=None, now=None, legs=None):
    """Проверить активные алерты пар codes (None — все). Вернуть [(пара, событие)].

    legs(pair, now) → [(имя ноги, время последней сделки)] — из памяти run_live.
    """
    now = now or dj_timezone.now()
    legs = legs or db_legs
    alerts = Alert.objects.select_related("pair").filter(status=Alert.ACTIVE, pair__active=True, kind__in=KINDS)
    if codes is not None:
        alerts = alerts.filter(pair__code__in=list(codes))
    by_pair = {}
    for alert in alerts:
        by_pair.setdefault(alert.pair_id, []).append(alert)
    sent, cache = [], {}
    chat = getattr(settings, "TELEGRAM_ALERT_CHAT_ID", "")
    for items in by_pair.values():
        pair = items[0].pair
        bar = SpreadBar.objects.select_related("contract").filter(pair=pair).order_by("-ts").first()
        if bar is None:
            continue
        removed = roll_off(pair, items, bar, now, log, chat)
        if removed:
            sent.append((pair.code, f"сняты перед склейкой: {len(removed)}"))
            items = [alert for alert in items if alert not in removed]
            if not items:
                continue
        found = [leg if isinstance(leg, Leg) else Leg(*leg) for leg in legs(pair, now)]
        if found and not same_day(found, now):
            continue  # ждём сделку по ноге сегодня: значение с прошлого дня не годится
        if found and not fresh(found, now):
            continue  # нога давно не торговалась (FRESH, у 6E/CNH/USDCNY/EURUSD — FRESH_SLOW): ждём сделку
        if not books_ok(found):
            continue  # в стакане ноги пусто: ждём, сторону не трогаем
        stamp = int(now.timestamp())
        spot = float(bar.spot or bar.perp_close or 0) or None
        for alert in items:
            raw = bar.raw_spread if alert.lines == "raw" else bar.spread_rate
            if raw is None:
                continue
            value = float(raw)
            side, where = state(alert, value, stamp, cache, spot)
            event = event_of(alert.kind, alert.side, side, alert.target)
            fields = {"side": side, "last_value": value}
            repeat = alert.repeat and alert.kind in REPEATABLE
            if event and repeat and alert.fired_at and now - alert.fired_at < timedelta(minutes=alert.pause_minutes):
                event = ""  # повторяющийся в паузе: сторону запомнить, сообщение не слать
            if event:
                text = message(alert, event, value, where, now, found, now)
                send(text, log, chat=chat, missing="TELEGRAM_ALERT_CHAT_ID", html=True)
                fields.update(status=Alert.ACTIVE if repeat else Alert.FIRED,
                              fired_at=now, fired_value=value, fired_text=plain(text))
                sent.append((pair.code, event))
            # В базу — только смена стороны и срабатывание: проверка идёт каждые 5 секунд.
            if side != alert.side or event:
                Alert.objects.filter(pk=alert.pk).update(**fields)
    return sent
