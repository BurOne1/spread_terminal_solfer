"""API графика: уровни и настройки приходят из браузера, в базу ничего не пишется."""

from django.test import TestCase

from strategy.models import EquityPoint
from strategy.rebuild import rebuild_spread
from strategy.tests import make_cny


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
