"""Группы «Аналитики» и «Зрители», публикация каналов, оповещения в Телеграм."""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import TestCase, override_settings

from charts import alerts
from charts.models import PublishedChannel
from market.models import Instrument, Pair
from strategy.models import SpreadBar

UTC = timezone.utc


def enter(client, group, password=""):
    return client.post("/enter/", {"group": group, "password": password})


@override_settings(ACCESS_DEFAULT_GROUP="", ANALYST_PASSWORD="secret")
class GroupTests(TestCase):
    def setUp(self):
        Pair.objects.filter(code="CNY").update(viewer=True)
        Pair.objects.create(code="HID", title="скрытая", future_root="HH", perp_code="", perp_symbol="", spot_code="", spot_symbol="")

    def test_first_visit_goes_to_welcome_and_remembers_group(self):
        self.assertRedirects(self.client.get("/"), "/enter/?next=/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/api/spread/").status_code, 401)
        self.assertContains(self.client.get("/enter/"), "Просмотр")
        self.assertRedirects(enter(self.client, "viewer"), "/", fetch_redirect_response=False)
        # Повторный заход — сразу график, страница входа сама уводит на него.
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertRedirects(self.client.get("/enter/"), "/", fetch_redirect_response=False)
        self.assertGreater(self.client.session.get_expiry_age(), 300 * 24 * 3600)

    def test_analyst_needs_password_and_password_change_logs_out(self):
        self.assertContains(enter(self.client, "analyst", "wrong"), "Неверный пароль")
        self.assertRedirects(enter(self.client, "analyst", "secret"), "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/analytics/").status_code, 200)
        with override_settings(ANALYST_PASSWORD="new"):
            self.assertEqual(self.client.get("/analytics/").status_code, 302)

    def test_viewer_is_closed_out_of_other_pages_and_pairs(self):
        enter(self.client, "viewer")
        for path in ("/analytics/", "/radar/"):
            self.assertRedirects(self.client.get(path), "/", fetch_redirect_response=False)
        for path in ("/api/analytics/?pair=CNY", "/api/radar/state/", "/api/analytics/export.xlsx"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.get("/api/spread/?pair=HID").status_code, 404)
        self.assertEqual(self.client.get("/api/spread/?pair=CNY").status_code, 200)
        codes = [row["code"] for row in self.client.get("/api/watchlist/").json()["pairs"]]
        self.assertEqual(codes, ["CNY"])
        page = self.client.get("/").content.decode()
        self.assertIn('class="viewer"', page)
        self.assertNotIn('<option value="HID"', page)
        # Публиковать зрителю нельзя.
        response = self.client.post("/api/published/", json.dumps({"pair": "CNY"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_viewer_gets_no_strategy_even_if_asked(self):
        enter(self.client, "viewer")
        with mock.patch("strategy.runner.run", side_effect=AssertionError("стратегия зрителю не нужна")):
            body = self.client.get("/api/spread/?pair=CNY&long=-1&short=1").json()
        self.assertEqual(body["pnl"], [])

    def test_leave_returns_to_welcome(self):
        enter(self.client, "viewer")
        self.assertRedirects(self.client.post("/leave/"), "/enter/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)


def channel_drawing(t0, t1, top0, top1, bottom0, drawing_id="dw_1"):
    return {"id": drawing_id, "kind": "parallel-channel", "style": {"color": "#2962ff"},
            "points": [{"time": t0, "price": top0}, {"time": t1, "price": top1}, {"time": t0, "price": bottom0}]}


class PublishTests(TestCase):
    """Тесты идут от группы «Аналитики» (ACCESS_DEFAULT_GROUP в settings_test)."""

    def test_publish_update_and_remove(self):
        drawing = channel_drawing(1_700_000_000, 1_700_036_000, 2.0, 1.0, 1.0)
        response = self.client.post("/api/published/", json.dumps({"pair": "CNY", "drawing": drawing, "lines": "raw"}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        listed = self.client.get("/api/published/?pair=CNY").json()
        self.assertEqual([(row["id"], row["lines"]) for row in listed["channels"]], [("dw_1", "raw")])
        # Повторная публикация того же рисунка — обновление, не вторая запись.
        drawing["points"][0]["price"] = 2.5
        self.client.post("/api/published/", json.dumps({"pair": "CNY", "drawing": drawing}), content_type="application/json")
        self.assertEqual(PublishedChannel.objects.get().data["points"][0]["price"], 2.5)
        # Только каналы.
        line = {"id": "dw_2", "kind": "trend-line", "points": [{"time": 1, "price": 1}, {"time": 2, "price": 2}]}
        self.assertEqual(self.client.post("/api/published/", json.dumps({"pair": "CNY", "drawing": line}),
                                          content_type="application/json").status_code, 400)
        self.client.delete("/api/published/", json.dumps({"pair": "CNY", "id": "dw_1"}), content_type="application/json")
        self.assertFalse(PublishedChannel.objects.exists())

    def test_spread_carries_published_version(self):
        before = self.client.get("/api/spread/?pair=CNY").json()["published"]
        PublishedChannel.objects.create(pair=Pair.objects.get(code="CNY"), drawing_id="x",
                                        data=channel_drawing(1, 2, 1, 1, 0))
        self.assertNotEqual(self.client.get("/api/spread/?pair=CNY").json()["published"], before)


class AlertTests(TestCase):
    def setUp(self):
        self.pair = Pair.objects.get(code="CNY")
        self.future = Instrument.objects.create(pair=self.pair, code="CRZ2026", kind="future")
        self.base = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)  # понедельник
        # Канал горизонтальный: низ 0, верх 1, середина 0.5.
        t0 = int(self.base.timestamp())
        self.channel = PublishedChannel.objects.create(
            pair=self.pair, drawing_id="dw", lines="rate", data=channel_drawing(t0, t0 + 3 * 3600, 1.0, 1.0, 0.0))

    def bar(self, hour, rate):
        SpreadBar.objects.update_or_create(pair=self.pair, ts=self.base + timedelta(hours=hour), defaults={
            "contract": self.future, "future_close": 1, "perp_close": 1, "spot": 1,
            "raw_spread": rate, "spread_rate": rate, "days_left": 30})

    def test_touches_and_middle_with_repeat_pause(self):
        texts = []
        with mock.patch.object(alerts, "send", side_effect=lambda text, log=None: texts.append(text) or True):
            self.bar(0, 0.3)
            self.assertEqual(alerts.check_channels(), [])  # первая проверка — только зона
            self.bar(1, 1.05)
            self.assertEqual(alerts.check_channels(), [("CNY", "above")])
            self.bar(2, 0.4)
            self.assertEqual(alerts.check_channels(), [("CNY", "mid_down")])
            self.bar(3, -0.1)
            self.assertEqual(alerts.check_channels(), [("CNY", "below")])
            self.bar(4, 0.2)
            self.bar(5, -0.2)
            # Снова ниже границы меньше чем через час — без повтора.
            self.assertEqual(alerts.check_channels(), [])
        self.assertIn("CNY", texts[0])
        self.assertIn("верхней границы", texts[0])

    def test_five_minute_channel_checked_on_five_minute_bars(self):
        from strategy.models import SpreadBarM5
        t0 = int(self.base.timestamp())
        PublishedChannel.objects.all().delete()
        channel = PublishedChannel.objects.create(pair=self.pair, drawing_id="m5", lines="rate", frame="M5",
                                                  data=channel_drawing(t0, t0 + 3 * 300, 1.0, 1.0, 0.0))
        for minutes, rate in ((0, 0.3), (5, 1.1)):
            SpreadBarM5.objects.create(pair=self.pair, ts=self.base + timedelta(minutes=minutes), contract=self.future,
                                       future_close=1, perp_close=1, spot=1, raw_spread=rate, spread_rate=rate, days_left=30)
        with mock.patch.object(alerts, "send", return_value=True) as sent:
            self.assertEqual(alerts.check_channels(base="H1"), [])  # часовой пересчёт 5-минутный канал не трогает
            channel.zone = "lower"
            channel.save()
            self.assertEqual(alerts.check_channels(base="M5"), [("CNY", "above")])
        self.assertIn("5м", sent.call_args[0][0])

    @override_settings(TELEGRAM_BOT_TOKEN="123:SECRET", TELEGRAM_CHAT_ID="-100500")
    def test_refusal_logs_reason_without_token(self):
        import httpx
        reply = httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"},
                               request=httpx.Request("POST", "https://api.telegram.org/bot123:SECRET/sendMessage"))
        lines = []
        with mock.patch("charts.alerts.httpx.post", return_value=reply):
            self.assertFalse(alerts.send("тест", lines.append))
        self.assertIn("chat not found", lines[0])
        self.assertIn("TELEGRAM_CHAT_ID", lines[0])
        self.assertNotIn("SECRET", lines[0])

    def test_channel_after_last_bar_counts_trading_hours(self):
        # Пятница 20:00 UTC → понедельник 04:00: между ними только сессии выходного дня
        # (сб и вс 07–15 UTC, по 9 часов), а не 56 календарных часов.
        friday = int(datetime(2026, 11, 20, 20, 0, tzinfo=UTC).timestamp())
        monday = int(datetime(2026, 11, 23, 4, 0, tzinfo=UTC).timestamp())
        self.assertEqual(alerts.index_of([friday], monday, "finam"), 19)
        self.assertEqual(alerts.index_of([friday], monday, "bybit"), 56)
