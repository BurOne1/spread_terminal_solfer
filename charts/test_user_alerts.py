"""Личные алерты по рисункам: charts/user_alerts.py и /api/alerts/."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from charts import user_alerts
from charts.models import Alert
from market.models import Instrument, Pair
from strategy.models import SpreadBar

UTC = timezone.utc
# Среда, 10:00 UTC (13:00 по Москве).
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
NO_LEGS = staticmethod(lambda pair, now: [])


def stamp(moment):
    return int(moment.timestamp())


class CheckTests(TestCase):
    def setUp(self):
        self.pair = Pair.objects.create(code="XX", title="тест", future_root="X", perp_code="", perp_symbol="",
                                        spot_code="", spot_symbol="", foreign_source="NONE")
        self.contract = Instrument.objects.create(pair=self.pair, code="XZ2026", finam_symbol="", kind="future")
        self.ts = NOW - timedelta(hours=30)

    def bar(self, value, raw=None):
        """Новый последний бар (часовой) со значением спреда value."""
        self.ts += timedelta(hours=1)
        SpreadBar.objects.create(
            pair=self.pair, ts=self.ts, contract=self.contract, future_close=Decimal(1), perp_close=Decimal(1),
            spot=Decimal(1), raw_spread=Decimal(str(raw if raw is not None else value)), days_left=10,
            spread_rate=Decimal(str(value)),
        )

    def alert(self, kind, points, **extra):
        return Alert.objects.create(pair=self.pair, drawing_id=kind, kind=kind,
                                    data={"id": kind, "kind": kind, "points": points, "style": extra.pop("style", {})}, **extra)

    def check(self, now=NOW, legs=None):
        return user_alerts.check_alerts(None, now=now, legs=legs or (lambda pair, moment: []))

    def test_horizontal_line_fires_once_on_any_cross(self):
        alert = self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        self.bar(4.0)
        self.assertEqual(self.check(), [])  # первая проверка только запоминает сторону
        alert.refresh_from_db()
        self.assertEqual((alert.side, alert.status), ("below", Alert.ACTIVE))
        self.bar(4.5)
        self.assertEqual(self.check(), [])
        self.bar(5.2)
        with mock.patch.object(user_alerts, "send") as send:
            self.assertEqual(self.check(), [("XX", "пересёк вверх")])
        self.assertIn("уровень 5.000", send.call_args[0][0])
        alert.refresh_from_db()
        self.assertEqual((alert.status, alert.fired_value), (Alert.FIRED, 5.2))
        # Сработавший больше не проверяется.
        self.bar(4.0)
        self.assertEqual(self.check(), [])

    def test_raw_line_and_touch(self):
        self.alert("horizontal-line", [{"time": stamp(NOW), "price": 2.0}], lines="raw")
        self.bar(10.0, raw=1.0)
        self.check()
        self.bar(10.0, raw=2.0)
        with mock.patch.object(user_alerts, "send"):
            self.assertEqual(self.check(), [("XX", "коснулся")])

    def test_trend_line_continues_right_and_moves_smoothly(self):
        start, end = NOW - timedelta(days=2), NOW - timedelta(days=1)
        later = self.alert("trend-line", [{"time": stamp(NOW + timedelta(hours=1)), "price": 1.0},
                                          {"time": stamp(NOW + timedelta(hours=2)), "price": 1.0}])
        segment = Alert.objects.create(pair=self.pair, drawing_id="seg", kind="trend-line", data={
            "id": "seg", "kind": "trend-line", "points": [{"time": stamp(start), "price": 1.0}, {"time": stamp(end), "price": 1.0}]})
        self.bar(0.5)
        self.check()
        segment.refresh_from_db()
        later.refresh_from_db()
        # Правее отрезка линия для алерта продолжается; левее первой точки — не действует.
        self.assertEqual((segment.side, later.side), ("below", ""))
        self.bar(1.5)
        with mock.patch.object(user_alerts, "send"):
            self.assertEqual(self.check(), [("XX", "пересёк вверх")])

    def test_position_inside_bar_and_after_last(self):
        times = [0, 300, 600]
        self.assertEqual(user_alerts.position(times, 450, "bybit", 5), 1.5)
        # После последнего бара: целые слоты и доля текущего (bybit торгует всегда).
        self.assertEqual(user_alerts.position(times, 600 + 300 + 150, "bybit", 5), 3.5)

    def test_channel_touch_and_rectangle_entry(self):
        start = NOW - timedelta(days=1)
        channel = self.alert("parallel-channel", [
            {"time": stamp(start), "price": 1.0}, {"time": stamp(NOW), "price": 1.0}, {"time": stamp(start), "price": 3.0},
        ])
        box = self.alert("rectangle", [{"time": stamp(start), "price": 4.0}, {"time": stamp(NOW + timedelta(days=1)), "price": 5.0}])
        self.bar(2.0)
        self.check()
        channel.refresh_from_db()
        box.refresh_from_db()
        self.assertEqual((channel.side, box.side), ("mid", "below"))  # 2.0 — ровно середина канала
        self.bar(4.2)
        with mock.patch.object(user_alerts, "send"):
            events = sorted(self.check())
        self.assertEqual(events, [("XX", "вошёл в зону"), ("XX", "коснулся верхней границы")])

    def test_waits_for_trade_of_every_leg_today(self):
        alert = self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        self.bar(4.0)
        today = [("SiZ6", NOW - timedelta(seconds=3)), ("CRZ6", NOW - timedelta(minutes=2))]
        self.check(legs=lambda pair, moment: today)
        self.bar(6.0)
        # Нога CR последний раз торговалась вчера (по Москве): алерт ждёт, сторону не трогает.
        stale = [("SiZ6", NOW - timedelta(seconds=3)), ("CRZ6", datetime(2026, 11, 17, 20, 0, tzinfo=UTC))]
        self.assertEqual(self.check(legs=lambda pair, moment: stale), [])
        alert.refresh_from_db()
        self.assertEqual((alert.side, alert.status), ("below", Alert.ACTIVE))
        with mock.patch.object(user_alerts, "send") as send:
            self.assertEqual(self.check(legs=lambda pair, moment: today), [("XX", "пересёк вверх")])
        self.assertNotIn("Ноги", send.call_args[0][0])  # ноги проверяются, но в текст не идут

    def test_waits_for_orders_in_every_book(self):
        alert = self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        fresh = NOW - timedelta(seconds=2)
        self.bar(4.0)
        self.check(legs=lambda pair, moment: [user_alerts.Leg("SiZ6", fresh, True), user_alerts.Leg("CRZ6", fresh, True)])
        self.bar(6.0)
        # В стакане CR пусто — ждём, сторону не трогаем.
        empty = [user_alerts.Leg("SiZ6", fresh, True), user_alerts.Leg("CRZ6", fresh, False)]
        self.assertEqual(self.check(legs=lambda pair, moment: empty), [])
        alert.refresh_from_db()
        self.assertEqual(alert.side, "below")
        # Радар про CR не знает — не мешает, но в сообщении сказано.
        unknown = [user_alerts.Leg("SiZ6", fresh, True), user_alerts.Leg("CRZ6", fresh, None)]
        with mock.patch.object(user_alerts, "send") as send:
            self.assertEqual(self.check(legs=lambda pair, moment: unknown), [("XX", "пересёк вверх")])
        self.assertNotIn("Стакан", send.call_args[0][0])  # стакан проверяется, но в сообщение не пишется

    def test_legs_from_radar_trades_and_books(self):
        from market import live
        from radar.models import BookState, Trade
        leg = Instrument.objects.create(pair=self.pair, code="XXF", finam_symbol="XXF@RTSX", kind="perp")
        other = Instrument.objects.create(pair=self.pair, code="YYF", finam_symbol="YYF@RTSX", kind="perp")
        from market.models import MinuteCandle
        MinuteCandle.objects.create(instrument=other, ts=NOW - timedelta(minutes=6), open=1, high=1, low=1, close=1)
        Trade.objects.create(symbol="XXF@RTSX", trade_id="1", ts=NOW - timedelta(seconds=4), price=1, size=1)
        BookState.objects.create(symbol="XXF@RTSX", asks=[[2, 1, False]], bids=[[1, 1, False]], updated_at=NOW - timedelta(seconds=30))
        BookState.objects.create(symbol="YYF@RTSX", asks=[], bids=[[1, 1, False]], updated_at=NOW - timedelta(minutes=10))
        live._legs_cache.clear()
        with mock.patch.object(live, "pair_symbols", return_value=["XXF@RTSX", "YYF@RTSX"]):
            legs = live.pair_legs(self.pair, NOW, memory=False)
        live._legs_cache.clear()
        self.assertEqual(legs[0], user_alerts.Leg("XXF", NOW - timedelta(seconds=4), True, False))
        # У YYF ни ленты, ни свежего стакана: время по 5-минутке, стакан неизвестен.
        self.assertEqual(legs[1], user_alerts.Leg("YYF", NOW - timedelta(minutes=6), None, True))
        self.assertIn("не позже 6 мин назад (по 5-минуткам)", user_alerts._leg_text(legs[1], NOW))

    def test_channel_middle_target_and_pause(self):
        start = NOW - timedelta(days=1)
        points = [{"time": stamp(start), "price": 1.0}, {"time": stamp(NOW), "price": 1.0}, {"time": stamp(start), "price": 3.0}]
        middle = self.alert("parallel-channel", points, target=Alert.MIDDLE)
        edges = Alert.objects.create(pair=self.pair, drawing_id="edges", kind="parallel-channel", target=Alert.EDGES,
                                     data={"id": "edges", "kind": "parallel-channel", "points": points})
        paused = Alert.objects.create(pair=self.pair, drawing_id="off", kind="horizontal-line", status=Alert.PAUSED,
                                      data={"id": "off", "kind": "horizontal-line", "points": [{"time": stamp(NOW), "price": 2.2}]})
        self.bar(1.5)
        self.check()
        self.bar(2.5)  # середина 2.0 пройдена вверх, до верхней границы 3.0 не дошёл
        with mock.patch.object(user_alerts, "send") as send:
            self.assertEqual(self.check(), [("XX", "пересёк середину вверх")])
        self.assertIn("середина 2.000", send.call_args[0][0])
        edges.refresh_from_db()
        paused.refresh_from_db()
        self.assertEqual((edges.status, paused.status, paused.side), (Alert.ACTIVE, Alert.PAUSED, ""))
        self.assertEqual(Alert.objects.get(pk=middle.pk).status, Alert.FIRED)

    def test_repeating_trend_line_waits_pause(self):
        start = NOW - timedelta(days=2)
        line = Alert.objects.create(pair=self.pair, drawing_id="t", kind="trend-line", repeat=True, pause_minutes=15, data={
            "id": "t", "kind": "trend-line", "points": [{"time": stamp(start), "price": 1.0}, {"time": stamp(start + timedelta(hours=1)), "price": 1.0}]})
        self.bar(0.5)
        self.check(now=NOW)
        self.bar(1.5)
        with mock.patch.object(user_alerts, "send"):
            self.assertEqual(self.check(now=NOW + timedelta(minutes=1)), [("XX", "пересёк вверх")])
            line.refresh_from_db()
            self.assertEqual(line.status, Alert.ACTIVE)  # повторяющийся не выключается
            self.bar(0.5)
            # Через 5 минут — в паузе: сторону запомнил, сообщения нет.
            self.assertEqual(self.check(now=NOW + timedelta(minutes=6)), [])
            line.refresh_from_db()
            self.assertEqual(line.side, "below")
            self.bar(1.5)
            self.assertEqual(self.check(now=NOW + timedelta(minutes=20)), [("XX", "пересёк вверх")])

    def test_level_in_percent_of_spot(self):
        Alert.objects.create(pair=self.pair, drawing_id="lvl", kind="level-pct", data={"id": "lvl", "kind": "level-pct", "share": 0.01})
        # spot у баров 1 → уровень 0.01; поднимем spot до 100 → уровень 1.0.
        self.bar(0.5)
        SpreadBar.objects.filter(pair=self.pair).update(spot=Decimal(100))
        self.check()
        self.bar(1.2)
        SpreadBar.objects.filter(pair=self.pair).update(spot=Decimal(100))
        with mock.patch.object(user_alerts, "send") as send:
            self.assertEqual(self.check(), [("XX", "пересёк вверх")])
        self.assertIn("пересёк вверх: уровень 1.000%", send.call_args[0][0])

    def test_alerts_off_four_hours_before_roll(self):
        line = self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        Instrument.objects.filter(pk=self.contract.pk).update(roll_at=NOW + timedelta(hours=5))
        self.bar(4.0)
        self.assertEqual(self.check(), [])  # за 5 ч — ещё работает
        with mock.patch.object(user_alerts, "send") as send:
            events = self.check(now=NOW + timedelta(hours=1, minutes=10))  # до склейки 3 ч 50 мин
        self.assertEqual(events, [("XX", "сняты перед склейкой: 1")])
        text = send.call_args[0][0]
        self.assertIn("скорая экспирация", text)
        self.assertIn("XZ2026 через 3 ч 50 мин", text)
        self.assertIn("#1 горизонтальная линия", text)
        line.refresh_from_db()
        self.assertEqual(line.status, Alert.PAUSED)
        # Включили руками до склейки — перед этой же склейкой второй раз не снимается.
        Alert.objects.filter(pk=line.pk).update(status=Alert.ACTIVE)
        with mock.patch.object(user_alerts, "send") as send:
            self.check(now=NOW + timedelta(hours=2))
        send.assert_not_called()
        self.assertEqual(Alert.objects.get(pk=line.pk).status, Alert.ACTIVE)

    def test_channel_middle_reached_exactly(self):
        self.assertEqual(user_alerts.channel_zone(2.0, 1.0, 2.0, 3.0), "mid")
        self.assertEqual(user_alerts.event_of("parallel-channel", "lower", "mid"), "достиг середины снизу")
        self.assertEqual(user_alerts.event_of("parallel-channel", "upper", "mid"), "достиг середины сверху")
        self.assertEqual(user_alerts.event_of("parallel-channel", "mid", "upper"), "")  # уже сработал на середине
        self.assertEqual(user_alerts.event_of("parallel-channel", "lower", "mid", Alert.EDGES), "")
        self.assertEqual(user_alerts.event_of("parallel-channel", "lower", "above"), "коснулся верхней границы")

    def test_waits_for_fresh_trades(self):
        alert = self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        ago = lambda seconds: NOW - timedelta(seconds=seconds)  # noqa: E731
        self.bar(4.0)
        self.check(legs=lambda pair, moment: [user_alerts.Leg("SiZ6", ago(1)), user_alerts.Leg("CRZ6", ago(1))])
        self.bar(6.0)
        # CR не торговался 2 мин 10 с — больше лимита 2 мин: ждём, сторону не трогаем.
        stale = [user_alerts.Leg("SiZ6", ago(1)), user_alerts.Leg("CRZ6", ago(130))]
        self.assertEqual(self.check(legs=lambda pair, moment: stale), [])
        self.assertEqual(Alert.objects.get(pk=alert.pk).side, "below")
        # CNH и USDCNY — лимит 4 мин: 3.5 мин ещё годится.
        slow = [user_alerts.Leg("SiZ6", ago(1)), user_alerts.Leg("CNHZ26", ago(210)), user_alerts.Leg("USDCNY", ago(210))]
        with mock.patch.object(user_alerts, "send"):
            self.assertEqual(self.check(legs=lambda pair, moment: slow), [("XX", "пересёк вверх")])
        self.assertFalse(user_alerts.fresh([user_alerts.Leg("6EZ26", ago(250))], NOW))
        # Время по 5-минутке (approx) — её начало: считается её концом (+5 мин).
        self.assertTrue(user_alerts.fresh([user_alerts.Leg("SiZ6", ago(400), None, True)], NOW))

    def test_bookless_spot_leg_is_named(self):
        self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        fresh = NOW - timedelta(seconds=2)
        legs = [user_alerts.Leg("CRZ6", fresh, True), user_alerts.Leg("USDCNY", fresh, None, bookless=True)]
        self.bar(4.0)
        self.check(legs=lambda pair, moment: legs)
        self.bar(6.0)
        with mock.patch.object(user_alerts, "send") as send:
            self.check(legs=lambda pair, moment: legs)
        self.assertTrue(send.call_args[0][0].endswith("\n\n18.11 10:00:00 UTC"))

    @override_settings(TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="published-chat", TELEGRAM_ALERT_CHAT_ID="my-chat")
    def test_goes_to_alert_chat(self):
        Pair.objects.filter(pk=self.pair.pk).update(title="a < b")
        self.alert("horizontal-line", [{"time": stamp(NOW), "price": 5.0}])
        self.bar(4.0)
        self.check()
        self.bar(6.0)
        with mock.patch("charts.alerts.httpx.post") as post:
            post.return_value.status_code = 200
            self.check()
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "my-chat")
        # Строка с колокольчиком и значение — жирным (HTML Телеграма), «<» в названии экранирован.
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["parse_mode"], "HTML")
        # Текст заголовка (эмодзи) — на вкус в message(); проверяется только разметка.
        title = sent["text"].split("\n")[0]
        self.assertTrue(title.startswith("<b>") and title.endswith("</b>") and "XX · a &lt; b" in title, title)
        self.assertIn("\nОчищенный спред пересёк вверх: уровень 5.000\n", sent["text"])
        fired = Alert.objects.get()
        self.assertIn("XX · a < b", fired.fired_text)
        self.assertNotIn("<b>", fired.fired_text)


class ApiTests(TestCase):
    def setUp(self):
        self.pair = Pair.objects.create(code="XX", title="тест", future_root="X", perp_code="", perp_symbol="",
                                        spot_code="", spot_symbol="", foreign_source="NONE")

    def send(self, method, payload):
        return getattr(self.client, method)("/api/alerts/", json.dumps(payload), content_type="application/json")

    def test_create_move_rearm_delete(self):
        drawing = {"id": "d1", "kind": "horizontal-line", "points": [{"time": 1, "price": 2.0}], "style": {"color": "#fff"}}
        response = self.send("post", {"pair": "XX", "drawing": drawing, "lines": "raw", "tf": "M5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["alert"]["lines"], response.json()["alert"]["status"]), ("raw", "active"))
        Alert.objects.update(side="below", status=Alert.FIRED, fired_value=2.1)
        # Сдвинули рисунок — сторона заново; сработавший остаётся сработавшим до ↻.
        moved = {**drawing, "points": [{"time": 1, "price": 3.0}]}
        self.send("post", {"pair": "XX", "drawing": moved})
        item = Alert.objects.get()
        self.assertEqual((item.side, item.status, item.lines), ("", Alert.FIRED, "raw"))
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1"}).json()["alert"]["status"], "active")
        # Шестерёнка: другой ряд — сторона заново, статус не меняется.
        Alert.objects.update(side="above")
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "lines": "rate"}).json()["alert"]["lines"], "rate")
        self.assertEqual((Alert.objects.get().side, Alert.objects.get().status), ("", Alert.ACTIVE))
        # Выключить и включить, цель канала.
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "paused": True}).json()["alert"]["status"], "paused")
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "paused": False}).json()["alert"]["status"], "active")
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "target": "middle"}).json()["alert"]["target"], "middle")
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "target": "x"}).status_code, 400)
        # Повтор — только у канала, трендовой линии и луча.
        self.assertEqual(self.send("patch", {"pair": "XX", "id": "d1", "repeat": True}).status_code, 400)
        level = self.send("post", {"pair": "XX", "level": {"share": -0.0015}, "lines": "raw"}).json()["alert"]
        self.assertEqual((level["kind"], level["data"]["share"], level["lines"]), ("level-pct", -0.0015, "raw"))
        self.assertEqual(self.send("patch", {"pair": "XX", "id": level["id"], "share": 0.002}).json()["alert"]["data"]["share"], 0.002)
        trend = {"id": "t1", "kind": "trend-line", "points": [{"time": 1, "price": 1.0}, {"time": 2, "price": 2.0}]}
        self.send("post", {"pair": "XX", "drawing": trend})
        body = self.send("patch", {"pair": "XX", "id": "t1", "repeat": True, "pause_minutes": 60}).json()["alert"]
        self.assertEqual((body["repeat"], body["pause_minutes"]), (True, 60))
        listed = self.client.get("/api/alerts/?pair=XX").json()["alerts"]
        self.assertIn("d1", [row["id"] for row in listed])
        self.assertEqual(self.send("delete", {"pair": "XX", "id": "d1"}).json()["removed"], 1)
        bad = {"id": "d2", "kind": "fib-retracement", "points": [{"time": 1, "price": 2.0}]}
        self.assertEqual(self.send("post", {"pair": "XX", "drawing": bad}).status_code, 400)

    def test_watchlist_marks_pairs_with_waiting_alerts(self):
        drawing = {"id": "w1", "kind": "horizontal-line", "points": [{"time": 1, "price": 2.0}]}
        self.send("post", {"pair": "XX", "drawing": drawing})
        rows = {row["code"]: row for row in self.client.get("/api/watchlist/").json()["pairs"]}
        self.assertEqual(rows["XX"]["alerts"], 1)
        Alert.objects.update(status=Alert.PAUSED)
        rows = {row["code"]: row for row in self.client.get("/api/watchlist/").json()["pairs"]}
        self.assertEqual(rows["XX"]["alerts"], 0)

    @override_settings(ACCESS_DEFAULT_GROUP="", ANALYST_PASSWORD="secret")
    def test_viewer_has_no_alerts(self):
        self.client.post("/enter/", {"group": "viewer"})
        self.assertEqual(self.client.get("/api/alerts/").status_code, 403)
        self.assertEqual(self.send("post", {"pair": "XX"}).status_code, 403)
        page = self.client.get("/").content.decode()
        self.assertNotIn('data-view="alerts"', page)


class LiveSocketTests(TestCase):
    """run_live: котировки по символу, без часовых баров, стакан ног алертов."""

    def test_subscriptions_and_book(self):
        import asyncio

        from market import live

        sent = []
        messages = [
            json.dumps({"type": "DATA", "subscription_type": "ORDER_BOOK", "payload": {"orderBook": [{
                "symbol": "XXF@RTSX",
                "rows": [{"price": {"value": "100"}, "buySize": {"value": "5"}, "action": "ACTION_ADD"},
                         {"price": {"value": "101"}, "sellSize": {"value": "3"}, "action": "ACTION_ADD"}],
            }]}}),
            json.dumps({"type": "DATA", "subscription_type": "ORDER_BOOK", "payload": {"orderBook": [{
                "symbol": "YYF@RTSX",
                "rows": [{"price": {"value": "50"}, "buySize": {"value": "1"}, "action": "ACTION_ADD"}],
            }]}}),
        ]

        class Socket:
            async def send(self, text):
                sent.append(json.loads(text))

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for message in messages:
                    yield message

        class Connect:
            async def __aenter__(self):
                return Socket()

            async def __aexit__(self, *args):
                return False

        with mock.patch.object(live.websockets, "connect", return_value=Connect()), \
                mock.patch.object(live, "_resolve", return_value={}):
            asyncio.run(live._socket(["XXF@RTSX", "YYF@RTSX"], "TIME_FRAME_H1", "jwt", lambda text: None, books=["XXF@RTSX", "YYF@RTSX"]))
        kinds = [(item["type"], json.dumps(item["data"], sort_keys=True)) for item in sent]
        self.assertNotIn("BARS", {kind for kind, _data in kinds})
        self.assertEqual([item["data"]["symbols"] for item in sent if item["type"] == "QUOTES"], [["XXF@RTSX"], ["YYF@RTSX"]])
        self.assertEqual([item["data"]["symbol"] for item in sent if item["type"] == "ORDER_BOOK"], ["XXF@RTSX", "YYF@RTSX"])
        # Обе стороны есть — True; только покупатели — False; не подписан — None.
        self.assertEqual((live.book_state("XXF@RTSX"), live.book_state("YYF@RTSX"), live.book_state("ZZ@RTSX")), (True, False, None))
        live._books.clear()


class LotsTests(TestCase):
    """charts/lots.py: стоимость лота = цена × STEPPRICE / MINSTEP, самая дорогая нога — 1 лот."""

    # Спецификации из ISS (20.11.2026): MINSTEP, STEPPRICE, PREVSETTLEPRICE.
    SPECS = {"GDZ6": (0.1, 8.1929, 3718.0), "SiZ6": (1.0, 1.0, 82456.0), "GLDRUBF": (0.1, 0.1, 9789.3), "CRZ6": (0.001, 1.0, 12.314)}

    def setUp(self):
        from charts import lots
        lots._cache.clear()

    def fetch(self, secids):
        return {secid: self.SPECS[secid] for secid in secids if secid in self.SPECS}

    def pair(self, code, **extra):
        return Pair.objects.create(code=code, title=code, future_root="X", perp_code="", perp_symbol="", spot_code="", spot_symbol="", **extra)

    def test_triangle_minimal_and_exact(self):
        from charts import lots
        with mock.patch("market.live.pair_symbols", return_value=["SiZ6@RTSX", "GDZ6@RTSX", "GLDRUBF@RTSX"]):
            text = lots.line(self.pair("TRIGD", foreign_source="TRI"), prices={}, fetch=self.fetch)
        # 1 лот GD ≈ 304 612 ₽: Si 3.69 → 4 (ошибка 8.3 %), точнее — 3 лота GD.
        self.assertTrue(text.startswith("Лоты: GDZ6 1 : SiZ6 4 : GLDRUBF 31 (1 лот GDZ6 ≈ 304 612 ₽, ошибка 8.3%)"), text)
        self.assertIn("точнее: GDZ6 3 : SiZ6 11 : GLDRUBF 93", text)

    def test_cross_counts_only_moex_legs_and_skips(self):
        from charts import lots
        # CRCNH: CR, CNH (CME), Si — CNH не в счёт.
        with mock.patch("market.live.pair_symbols", return_value=["CNHZ26@XCME", "CRZ6@RTSX", "SiZ6@RTSX"]):
            text = lots.line(self.pair("CRCNH", foreign_source="CMEFX"), prices={}, fetch=self.fetch)
        self.assertTrue(text.startswith("Лоты: SiZ6 1 : CRZ6 7"), text)
        self.assertNotIn("CNH", text)
        self.assertEqual(lots.line(self.pair("SICAL", foreign_source="CAL"), prices={}, fetch=self.fetch), "")
        self.assertIn("Мосбиржа 100 : NYMEX 1", lots.line(self.pair("NGNYMEX", foreign_source="NGNYM"), prices={}, fetch=self.fetch))


class LotsApiTests(TestCase):
    def test_calendar_note_and_viewer_closed(self):
        Pair.objects.create(code="SICALX", title="кал", future_root="Si", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CAL")
        body = self.client.get("/api/lots/?pair=SICALX").json()
        self.assertEqual(body["legs"], [])
        self.assertIn("календарь", body["note"])
        self.assertEqual(self.client.get("/api/lots/?pair=NOPE").status_code, 404)
        with override_settings(ACCESS_DEFAULT_GROUP="", ANALYST_PASSWORD="secret"):
            self.client.post("/enter/", {"group": "viewer"})
            self.assertEqual(self.client.get("/api/lots/?pair=SICALX").status_code, 403)


class FundingPanelTests(TestCase):
    """rates/expected.py: SWAPRATE из ISS, история FundingDay на единицу, % годовых по цене."""

    def setUp(self):
        from rates import expected
        expected._cache.clear()

    def test_gold_perp_panel(self):
        from datetime import date

        from rates.expected import panel
        from rates.models import FundingDay
        gold, _ = Pair.objects.get_or_create(code="GOLD", defaults={"title": "золото", "future_root": "GL", "foreign_source": "EWMA14"})
        Pair.objects.filter(pk=gold.pk).update(perp_code="GLDRUBF")
        gold.refresh_from_db()
        # В базе — SWAPRATE × лот; у золота лот 1 грамм.
        for offset, value in enumerate([2.49783, 2.6, 2.7, 6.0, 6.1, 5.5, 6.2, 7.0, 7.1, 7.3]):
            FundingDay.objects.update_or_create(date=date(2026, 11, 19) - timedelta(days=offset), defaults={"swaprate_gold": Decimal(str(value))})
        data = panel(gold, fetch=lambda secid: {"swaprate": 3.11116, "last": 9592.4, "settle": 9593.7, "time": "2026-11-20 13:18:02"})
        self.assertEqual((data["perp"], data["expected"]), ("GLDRUBF", 3.11116))
        self.assertAlmostEqual(data["annual"], 3.11116 / 9592.4 * 365 * 100, places=6)  # ≈ 11.84 %
        rows = {row["name"]: row["value"] for row in data["rows"]}
        self.assertAlmostEqual(rows["F_lastday"], 2.49783, places=4)  # в FundingDay — 4 знака
        self.assertAlmostEqual(rows["F_avg3d"], (2.49783 + 2.6 + 2.7) / 3, places=4)
        self.assertIn("F_avg10d", rows)

    def test_no_funding_for_calendar_and_api_closed_for_viewer(self):
        from rates.expected import panel
        calendar = Pair.objects.create(code="SICALF", title="к", future_root="Si", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CAL")
        self.assertEqual(panel(calendar)["perp"], "")
        self.assertEqual(self.client.get("/api/funding/?pair=SICALF").json()["perp"], "")
        with override_settings(ACCESS_DEFAULT_GROUP="", ANALYST_PASSWORD="secret"):
            self.client.post("/enter/", {"group": "viewer"})
            self.assertEqual(self.client.get("/api/funding/?pair=SICALF").status_code, 403)
