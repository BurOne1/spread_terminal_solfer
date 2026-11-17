"""Оповещения в Телеграм по опубликованным каналам: касание границ и переход середины.

Границы — как на графике: канал прямой по номеру бара, а не по времени
(ночи и выходные шкала пропускает). Точки канала после последнего бара
считаются по торговым часам, как пустые слоты графика (tradingHour в home.html).
Значение — последний бар пары: сырой спред (lines=raw) или очищенный.

Зоны: above (на верхней границе и выше), upper (верхняя половина), lower,
below. Событие — вход в above или below (касание границы) и переход середины.
Первая проверка после публикации только запоминает зону. Одно и то же событие
не чаще раза в REPEAT: у границы спред может дрожать туда-обратно.

Отправка — Bot API напрямую (httpx), без отдельного процесса бота:
проверку вызывает run_live после пересчёта пар.
"""

from bisect import bisect_left
from datetime import datetime, timedelta, timezone

import httpx
from django.conf import settings
from django.utils import timezone as dj_timezone

from charts.models import PublishedChannel
from strategy.models import SpreadBar

REPEAT = timedelta(hours=1)
HOUR = 3600
EVENTS = {
    "above": "коснулся верхней границы",
    "below": "коснулся нижней границы",
    "mid_up": "пересёк середину вверх",
    "mid_down": "пересёк середину вниз",
}


def trading_hour(stamp, venue):
    """Как tradingHour на странице: Bybit — круглосуточно, Мосбиржа — будни 04–20 UTC."""
    if venue == "bybit":
        return True
    moment = datetime.fromtimestamp(stamp, timezone.utc)
    return moment.weekday() < 5 and 4 <= moment.hour <= 20


def index_of(times, stamp, venue):
    """Номер бара для времени точки канала: ближайший бар или пустой слот после последнего."""
    if not times:
        return None
    if stamp <= times[-1]:
        position = bisect_left(times, stamp)
        if position == 0:
            return 0
        if position >= len(times):
            return len(times) - 1
        return position if times[position] - stamp < stamp - times[position - 1] else position - 1
    slots, moment = 0, times[-1] + HOUR
    while moment <= stamp:
        if trading_hour(moment, venue):
            slots += 1
        moment += HOUR
    return len(times) - 1 + slots


def bounds(points, times, venue, index):
    """(низ, середина, верх) канала в номере бара index."""
    (p0, p1, p2) = points[:3]
    i0, i1, i2 = (index_of(times, point["time"], venue) for point in (p0, p1, p2))
    if None in (i0, i1, i2):
        return None
    slope = 0.0 if i1 == i0 else (p1["price"] - p0["price"]) / (i1 - i0)
    main = p0["price"] + slope * (index - i0)
    other = main + (p2["price"] - (p0["price"] + slope * (i2 - i0)))
    lower, upper = min(main, other), max(main, other)
    return lower, (lower + upper) / 2, upper


def zone_of(value, lower, middle, upper):
    if value >= upper:
        return "above"
    if value <= lower:
        return "below"
    return "upper" if value > middle else "lower"


def event_of(before, after):
    if not before or before == after:
        return ""
    if after in ("above", "below"):
        return after
    if before in ("lower", "below") and after == "upper":
        return "mid_up"
    if before in ("upper", "above") and after == "lower":
        return "mid_down"
    return ""


def send(text, log=None):
    """Сообщение в чат TELEGRAM_CHAT_ID. Без токена — только в лог."""
    token, chat = settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID
    if not token or not chat:
        if log:
            log("телеграм не настроен (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID): " + text.replace("\n", " | "))
        return False
    # Токен — часть адреса Bot API: в логи идёт только причина от Телеграма, без адреса.
    try:
        response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=10.0)
    except httpx.HTTPError as exc:
        if log:
            log(f"телеграм недоступен: {type(exc).__name__}")
        return False
    if response.status_code == 200:
        return True
    if log:
        try:
            reason = response.json().get("description", "")
        except ValueError:
            reason = ""
        log(f"телеграм не принял сообщение ({response.status_code}): {reason or 'без пояснения'}" + _hint(reason))
    return False


def _hint(reason):
    """Подсказка к частым отказам Bot API."""
    text = reason.lower()
    if "chat not found" in text:
        return (". Проверьте TELEGRAM_CHAT_ID: бот должен быть добавлен в этот чат (в канал — администратором), "
                "у группы номер вида -100…, у канала можно @имя")
    if "unauthorized" in text or "not found" == text.strip():
        return ". Проверьте TELEGRAM_BOT_TOKEN"
    if "not enough rights" in text or "forbidden" in text:
        return ". Боту не хватает прав писать в этот чат"
    return ""


def _number(value):
    return f"{value:,.3f}".replace(",", " ")


def message(channel, event, value, lower, middle, upper, stamp):
    pair = channel.pair
    line = "сырой спред" if channel.lines == "raw" else "очищенный спред"
    border = {"above": upper, "below": lower}.get(event, middle)
    return (
        f"{pair.code} · {pair.title}\n"
        f"{line[0].upper() + line[1:]} {_number(value)} {EVENTS[event]} канала ({_number(border)}).\n"
        f"Канал: низ {_number(lower)} · середина {_number(middle)} · верх {_number(upper)} {pair.quote}\n"
        f"{stamp:%d.%m %H:%M} UTC"
    )


def check_channels(codes=None, log=None, now=None):
    """Проверить опубликованные каналы пар codes (None — все). Вернуть отправленные события."""
    now = now or dj_timezone.now()
    channels = PublishedChannel.objects.select_related("pair").filter(pair__active=True)
    if codes is not None:
        channels = channels.filter(pair__code__in=list(codes))
    sent = []
    for channel in channels:
        points = (channel.data or {}).get("points") or []
        if len(points) < 3:
            continue
        since = datetime.fromtimestamp(min(point["time"] for point in points[:3]), timezone.utc) - timedelta(days=3)
        rows = list(
            SpreadBar.objects.filter(pair=channel.pair, ts__gte=since).order_by("ts")
            .values_list("ts", "raw_spread", "spread_rate")
        )
        if not rows:
            continue
        times = [int(ts.timestamp()) for ts, _raw, _rate in rows]
        found = bounds(points, times, channel.pair.venue, len(times) - 1)
        if found is None:
            continue
        lower, middle, upper = found
        ts, raw, rate = rows[-1]
        value = float(raw if channel.lines == "raw" else rate)
        zone = zone_of(value, lower, middle, upper)
        event = event_of(channel.zone, zone)
        fields = {"zone": zone}
        repeated = event and channel.event == event and channel.event_at and now - channel.event_at < REPEAT
        if event and not repeated:
            send(message(channel, event, value, lower, middle, upper, ts), log)
            fields.update(event=event, event_at=now)
            sent.append((channel.pair.code, event))
        if fields["zone"] != channel.zone or "event" in fields:
            PublishedChannel.objects.filter(pk=channel.pk).update(**fields)
    return sent
