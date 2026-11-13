"""API графика: уровни и настройки приходят из браузера, в базу ничего не пишется."""

from django.test import TestCase

from strategy.models import EquityPoint, SpreadBar
from strategy.rebuild import rebuild_spread
from strategy.tests import make_cny, make_usd


class SpreadApiTests(TestCase):
    def setUp(self):
        make_cny()
        rebuild_spread("CNY")

    def test_user_levels_and_open_basis(self):
        response = self.client.get(
            "/api/spread/?pair=CNY&long=-1,-1.5&short=1&ltp=0.25&stp=-0.25&entry=reverse&average=0&basis=open"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["levels"]["long_pcts"], [-0.01, -0.015])
        self.assertEqual(body["levels"]["short_tp_pcts"], [-0.0025])
        self.assertEqual(body["basis"], "open")
        self.assertAlmostEqual(body["points"][0]["raw"], 0.2, places=6)
        self.assertEqual(len(body["pnl"]), 5)
        self.assertIn("position", body)
        self.assertFalse(EquityPoint.objects.exists())

    def test_without_params_uses_pair_levels(self):
        body = self.client.get("/api/spread/?pair=CNY").json()
        self.assertEqual(body["levels"]["long_pcts"], body["pair_levels"]["long_pcts"])
        self.assertAlmostEqual(body["points"][0]["raw"], 0.3, places=6)

    def test_empty_param_means_no_levels(self):
        body = self.client.get("/api/spread/?pair=CNY&long=&short=1&ltp=&stp=").json()
        self.assertEqual(body["levels"]["long_pcts"], [])
        self.assertEqual(body["levels"]["short_pcts"], [0.01])

    def test_watchlist(self):
        body = self.client.get("/api/watchlist/?basis=open").json()
        cny = next(item for item in body["pairs"] if item["code"] == "CNY")
        self.assertEqual(cny["contract"], "CRZ2026")
        self.assertAlmostEqual(cny["raw"], 0.2, places=6)
        self.assertIsNone(cny["change"])
        self.assertEqual(cny["title"], "CR − CNYRUBF")

    def test_spread_reports_venue(self):
        self.assertEqual(self.client.get("/api/spread/?pair=CNY").json()["venue"], "finam")

    def test_home_renders(self):
        response = self.client.get("/?pair=CNY")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="basis"')
        for part in ("topbar", "drawbar", "chart-wrap", "side", "rail", "watchlist", "statusbar", "legend-0", "theme"):
            self.assertContains(response, f'id="{part}"')


class CalendarApiTests(TestCase):
    def setUp(self):
        make_usd()
        rebuild_spread("SICAL")

    def test_calendar_labels_and_candles(self):
        body = self.client.get("/api/spread/?pair=SICAL").json()
        self.assertEqual(body["labels"]["SiZ2026"], "Z2026/H2027")
        self.assertEqual(body["funding"], [])
        self.assertIn("секонд − фронт", body["subtitle"])
        first = body["points"][0]
        self.assertEqual(first["contract"], "SiZ2026")
        self.assertAlmostEqual(first["candle"]["c"], 2.3, places=6)
        self.assertAlmostEqual(first["candle"]["o"], 2.0, places=6)
        self.assertGreater(body["levels"]["spot"], 0)

    def test_watchlist_shows_window_pair(self):
        body = self.client.get("/api/watchlist/").json()
        item = next(row for row in body["pairs"] if row["code"] == "SICAL")
        self.assertEqual(item["contract"], "H2027/M2027")
        self.assertEqual(item["group"], "second")
        self.assertEqual(next(row for row in body["pairs"] if row["code"] == "USD")["group"], "front")


class SpreadEtagTests(TestCase):
    def setUp(self):
        from charts import views
        views._spread_cache.clear()
        make_cny()
        rebuild_spread("CNY")

    def test_unchanged_bars_give_304_and_rebuild_gives_new_body(self):
        from unittest import mock
        from charts import views
        first = self.client.get("/api/spread/?pair=CNY")
        tag = first["ETag"]
        self.assertEqual(first.json()["version"].strip('"'), tag.strip('"').removeprefix("W/").strip('"'))
        self.assertEqual(self.client.get("/api/spread/?pair=CNY", HTTP_IF_NONE_MATCH=tag).status_code, 304)
        # Другие настройки (уровни, цена часа) — сразу свой расчёт.
        self.assertEqual(self.client.get("/api/spread/?pair=CNY&basis=open", HTTP_IF_NONE_MATCH=tag).status_code, 200)
        rebuild_spread("CNY", since=SpreadBar.objects.order_by("-ts").first().ts)
        # Бары сдвинулись, но с прошлого расчёта меньше SPREAD_MIN_AGE: прежний ответ, 304.
        with mock.patch.object(views, "SPREAD_MIN_AGE", 30):
            self.assertEqual(self.client.get("/api/spread/?pair=CNY", HTTP_IF_NONE_MATCH=tag).status_code, 304)
        fresh = self.client.get("/api/spread/?pair=CNY", HTTP_IF_NONE_MATCH=tag)
        self.assertEqual(fresh.status_code, 200)
        self.assertNotEqual(fresh["ETag"], tag)

    def test_last_pair_from_cookie(self):
        from charts.views import last_pair_code
        from django.test import RequestFactory
        request = RequestFactory().get("/")
        request.COOKIES["spread_pair"] = "CNY"
        self.assertEqual(last_pair_code(request), "CNY")
        request.COOKIES["spread_pair"] = "NOPE"
        self.assertEqual(last_pair_code(request), "")
        # Адрес важнее cookie.
        request = RequestFactory().get("/?pair=CNY")
        request.COOKIES["spread_pair"] = "NOPE"
        self.assertEqual(last_pair_code(request), "CNY")


class UnknownPairTests(TestCase):
    def test_unknown_pair_is_not_replaced_by_first(self):
        response = self.client.get("/api/spread/?pair=NOPE")
        self.assertEqual(response.status_code, 404)
        self.assertIn("NOPE", response.json()["error"])
        self.assertEqual(self.client.get("/api/spread/").status_code, 200)


class BrentApiTests(TestCase):
    def setUp(self):
        from strategy.tests import make_brent
        make_brent()
        rebuild_spread("BRNYMEX")

    def test_labels_candles_and_levels(self):
        body = self.client.get("/api/spread/?pair=BRNYMEX").json()
        self.assertEqual(body["labels"]["BRZ2026"], "BRZ6/BZF27")
        self.assertEqual(body["funding"], [])
        first = body["points"][0]
        self.assertEqual(first["contract"], "BRZ2026")
        self.assertAlmostEqual(first["candle"]["c"], 0.12, places=6)
        self.assertAlmostEqual(first["candle"]["o"], 0.10, places=6)
        self.assertAlmostEqual(body["levels"]["spot"], 72.0, places=6)

    def test_watchlist(self):
        body = self.client.get("/api/watchlist/").json()
        item = next(row for row in body["pairs"] if row["code"] == "BRNYMEX")
        self.assertEqual((item["contract"], item["group"]), ("BRF7/BZG27", "front"))
