"""Аналитика контрактов: сетки на синтетике, дневные ряды из SpreadBar, API, загрузка TradingView."""

import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from analytics import core, data, views
from analytics.data import contract_month, contracts
from market.models import Candle, Instrument, Pair
from strategy.models import SpreadBar

UTC = timezone.utc


def falling(width=30, scale=1.0, noise=0.0):
    """Спред, линейно сходящийся к нулю к экспирации: s(d) = scale × d / 10."""
    return [scale * day / 10 + noise * (day % 3 - 1) for day in range(width)]


class CoreTests(SimpleTestCase):
    def setUp(self):
        self.S = [falling(scale=1.0), falling(scale=2.0, noise=0.01), falling(scale=0.5, noise=0.02), falling(scale=1.5)]
        self.P = [[100.0] * 30 for _ in self.S]

    def test_returns_grid_sells_converging_spread(self):
        G, best = core.returns_grid(self.S, self.P, min_hold=5, min_years=2, smooth_half=1)
        self.assertIsNotNone(best)
        din, dout = best
        self.assertGreater(din - dout, 4)
        self.assertEqual(G["side"][din][dout], 1)
        self.assertGreater(G["ret"][din][dout], 0)
        self.assertEqual(G["win"][din][dout], 1.0)
        # Вход за 20 → выход за 0: падение s(20) − s(0) = 2 × масштаб, среднее по 4 контрактам 2.5 → 2.5 % капитала 100.
        self.assertAlmostEqual(G["ret"][20][0], 2.5, delta=0.05)
        self.assertTrue(core.finite(G["mae"][20][0]))
        self.assertFalse(core.finite(G["ret"][3][0]))  # удержание меньше min_hold

    def test_move_and_direction_grids(self):
        M, best_move = core.move_grid(self.S, min_hold=5, min_years=2, smooth_half=0)
        D, best_side = core.side_grid(self.S, min_hold=5, min_years=2, smooth_half=0)
        self.assertGreater(M["move"][20][0], 0)
        self.assertLess(D["move"][20][0], 0)
        self.assertEqual(D["agree"][20][0], 1.0)
        self.assertIsNotNone(best_move)
        self.assertIsNotNone(best_side)

    def grids(self, S):
        P = [[100.0] * len(S[0]) for _ in S]
        G, _ = core.returns_grid(S, P, min_hold=5, min_years=2, smooth_half=1)
        M, _ = core.move_grid(S, min_hold=5, min_years=2, smooth_half=1)
        D, _ = core.side_grid(S, min_hold=5, min_years=2, smooth_half=1)
        return G, M, D

    def test_zones_short_for_falling_spread(self):
        G, M, D = self.grids(self.S)
        Z, _ = core.zscore_grid(self.S, min_hold=5, min_years=2, smooth_half=1)
        found = core.zones(G, D, Z, M, share=0.5)
        self.assertTrue(found)
        zone = found[0]
        # Спред падал: шорт — продаём на входе (дальше от экспирации), покупаем на выходе.
        self.assertEqual(zone["side"], "шорт")
        self.assertEqual((zone["sell"], zone["buy"]), (zone["entry"], zone["exit"]))
        self.assertGreater(zone["sell"][1], zone["buy"][0])
        self.assertGreaterEqual(zone["cells"], 4)
        din, dout = zone["best"]
        self.assertTrue(zone["entry"][0] <= din <= zone["entry"][1] and zone["exit"][0] <= dout <= zone["exit"][1])
        self.assertGreater(zone["ret"], 0)

    def test_zones_long_for_rising_spread_and_threshold(self):
        S = [[-value for value in row] for row in self.S]
        G, M, D = self.grids(S)
        found = core.zones(G, D, None, M, share=0.5)
        self.assertEqual({zone["side"] for zone in found}, {"лонг"})
        self.assertEqual(found[0]["buy"], found[0]["entry"])
        # Чем выше порог, тем меньше клеток в зонах.
        wide = sum(zone["cells"] for zone in core.zones(G, D, None, M, share=0.2))
        narrow = sum(zone["cells"] for zone in core.zones(G, D, None, M, share=0.9))
        self.assertGreater(wide, narrow)

    def test_zones_need_direction_to_agree(self):
        G, M, D = self.grids(self.S)
        # «Куда шёл спред» против стороны сделки — зон нет.
        flipped = {**D, "smooth": [[None if v is None else (-v if core.finite(v) else v) for v in row] for row in D["smooth"]]}
        self.assertEqual(core.zones(G, flipped, None, M), [])

    def test_ratio_is_capped_when_spread_of_returns_is_tiny(self):
        S = [falling(scale=1.0), falling(scale=1.0)]
        P = [[100.0] * 30 for _ in S]
        G, _ = core.returns_grid(S, P, min_hold=5, min_years=2, smooth_half=0, metric="ratio")
        # Одинаковые годы: разброс 0, раньше доход/разброс — nan или 1e15; теперь не выше 10.
        self.assertAlmostEqual(G["ratio"][20][0], 10.0, places=6)
        self.assertTrue(core.finite(G["smooth"][20][0]))

    def test_zscore_ignores_level_and_sees_trend(self):
        S = [falling(width=60, scale=k, noise=0.05) for k in (1.0, 1.5, 0.8, 1.2)]
        Z, best = core.zscore_grid(S, min_hold=5, min_years=2, smooth_half=0)
        self.assertLess(Z["move"][20][0], 0)  # спред падал — шорт
        self.assertEqual(Z["agree"][20][0], 1.0)
        self.assertIsNotNone(best)
        # Сдвиг уровня (спред далеко от нуля) z не меняет: в отличие от «движения к нулю».
        shifted = [[value + 50 for value in row] for row in S]
        Z2, _ = core.zscore_grid(shifted, min_hold=5, min_years=2, smooth_half=0)
        self.assertAlmostEqual(Z2["move"][20][0], Z["move"][20][0], places=9)
        # Ближе к началу окна, чем lookback, σ ещё не набрана — клетки нет.
        self.assertFalse(core.finite(Z["move"][58][0]))

    def test_local_sigma_looks_only_before_entry(self):
        row = [0.0] * 10 + [float(k % 2) for k in range(30)]
        sigma = core._local_sigma(row, lookback=5)
        # Дни 0…4: изменения только в нулевом участке — σ = 0, всплеск дальше не виден.
        self.assertEqual(sigma[0], 0.0)
        self.assertGreater(sigma[12], 0)

    def test_clean_replaces_nan(self):
        self.assertEqual(core.clean({"a": [core.NAN, 1.234567891]}), {"a": [None, 1.23457]})

    def test_contract_month(self):
        roll = datetime(2026, 11, 25, tzinfo=UTC)
        self.assertEqual(contract_month("CRZ2026", None), ("Z", 2026))
        self.assertEqual(contract_month("6EH2027", None), ("H", 2027))
        self.assertEqual(contract_month("CRZ2026.CRCNH", None), ("Z", 2026))
        self.assertEqual(contract_month("NGX6NGZ6", roll), ("X", 2026))


def make_history(years=(2022, 2023, 2024, 2025), live=2026):
    """Пара с декабрьскими кварталами: окно 40 дней, спред = days_left / 10, по 4 часа в день."""
    pair = Pair.objects.create(code="TEST", title="тест", future_root="TT", perp_code="", perp_symbol="", spot_code="", spot_symbol="")
    for year in (*years, live):
        roll = datetime(year, 12, 17, 20, 50, tzinfo=UTC)
        instrument = Instrument.objects.create(pair=pair, code=f"TTZ{year}", kind="future", roll_at=roll, listed_at=roll - timedelta(days=40))
        bars = []
        for left in range(40, 0, -1):
            day = roll - timedelta(days=left)
            for hour in (8, 10, 12, 14):
                value = left / 10 * (1 + (year % 3) * 0.2)
                bars.append(SpreadBar(
                    pair=pair, ts=day.replace(hour=hour, minute=0), contract=instrument, future_close=100, perp_close=100,
                    spot=100, raw_spread=value, spread_rate=value / 2, days_left=left,
                ))
        SpreadBar.objects.bulk_create(bars)
    return pair


class AnalyticsApiTests(TestCase):
    def setUp(self):
        views._cache.clear()
        self.pair = make_history()

    def test_daily_series_per_contract(self):
        found = contracts(self.pair, now=datetime(2026, 11, 20, tzinfo=UTC))
        self.assertEqual([item["code"] for item in found], ["TTZ2022", "TTZ2023", "TTZ2024", "TTZ2025", "TTZ2026"])
        self.assertTrue(found[-1]["live"])
        self.assertFalse(found[0]["live"])
        self.assertEqual(len(found[0]["points"]), 40)
        self.assertEqual(found[0]["points"][0][0], 1)

    def test_page_contracts_and_grids(self):
        self.assertContains(self.client.get("/analytics/"), "plot-direction")
        body = self.client.get("/api/analytics/contracts/?pair=TEST").json()
        self.assertEqual(body["months"], [{"code": "Z", "name": "декабрь"}])
        body = self.client.get("/api/analytics/?pair=TEST&hold=5&smooth=1").json()
        self.assertNotIn("note", body)
        self.assertEqual(body["used"], ["TTZ2022", "TTZ2023", "TTZ2024", "TTZ2025"])
        self.assertEqual(len(body["lines"]), 5)
        self.assertEqual(body["width"], 41)
        din, dout = body["best"]["returns"]
        self.assertEqual(body["returns"]["side"][din][dout], 1)
        self.assertLess(body["direction"]["move"][30][0], 0)
        self.assertTrue(body["ranges"])

    def test_zones_in_api_and_excel(self):
        import io
        import zipfile
        body = self.client.get("/api/analytics/?pair=TEST&hold=5&share=60").json()
        self.assertEqual(body["params"]["share"], 60)
        self.assertEqual(body["ranges"][0]["side"], "шорт")
        response = self.client.get("/api/analytics/export.xlsx?pair=TEST&hold=5&share=60")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn('filename="analytics_TEST.xlsx"', response["Content-Disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertIn("xl/worksheets/sheet2.xml", archive.namelist())
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("покупка, дн. до эксп.", sheet)
        self.assertIn("шорт", sheet)

    def test_zone_dates_for_chosen_month(self):
        # Без месяца — только дни до экспирации.
        body = self.client.get("/api/analytics/?pair=TEST&hold=5").json()
        self.assertNotIn("target", body)
        self.assertNotIn("buy_dates", body["ranges"][0])
        body = self.client.get("/api/analytics/?pair=TEST&hold=5&month=Z").json()
        zone = body["ranges"][0]
        roll = datetime.fromisoformat(body["target"]["roll"]).date()
        # Даты = склейка − дней до экспирации; у ранней границы больше дней.
        self.assertEqual(datetime.fromisoformat(zone["sell_dates"][0]).date(), roll - timedelta(days=zone["sell"][1]))
        self.assertEqual(datetime.fromisoformat(zone["buy_dates"][1]).date(), roll - timedelta(days=zone["buy"][0]))
        self.assertIn("late", zone)
        response = self.client.get("/api/analytics/export.xlsx?pair=TEST&hold=5&month=Z")
        import io
        import zipfile
        sheet = zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("покупка, даты", sheet)

    def test_target_roll_live_or_estimated(self):
        found = contracts(self.pair, now=datetime(2026, 11, 20, tzinfo=UTC))
        # Идущий TTZ2026 — его склейка.
        self.assertEqual(views.target_roll(found, now=datetime(2026, 11, 20, tzinfo=UTC)),
                         ("TTZ2026", datetime(2026, 12, 17).date(), False))
        # Идущего нет — прошлая склейка, сдвинутая на год вперёд.
        expired = [item for item in found if item["code"] != "TTZ2026"]
        self.assertEqual(views.target_roll(expired, now=datetime(2026, 11, 20, tzinfo=UTC)),
                         ("Z2026", datetime(2026, 12, 17).date(), True))

    def test_zscore_in_api(self):
        body = self.client.get("/api/analytics/?pair=TEST&hold=5").json()
        self.assertEqual(len(body["zscore"]["smooth"]), body["width"])
        self.assertIn("zscore", body["best"])

    def test_depth_in_contract_periods(self):
        from unittest import mock
        from market.models import Pair as PairModel
        # Период по типу пары: квартал 92 дня, месяц 31.
        self.assertEqual(views.period_days(self.pair), 92)
        self.assertEqual(views.period_days(PairModel(foreign_source="NYMEX")), 31)
        with mock.patch.object(views, "period_days", return_value=15):
            one = self.client.get("/api/analytics/?pair=TEST&hold=5&periods=1").json()
            two = self.client.get("/api/analytics/?pair=TEST&hold=5&periods=2").json()
            cut = self.client.get("/api/analytics/?pair=TEST&hold=5&periods=2&max_days=20").json()
        self.assertEqual((one["width"], two["width"], cut["width"]), (16, 31, 21))
        self.assertEqual(one["period_days"], 15)
        self.assertLessEqual(max(max(line["x"]) for line in one["lines"]), 15)

    def test_exclusions_and_short_history(self):
        body = self.client.get("/api/analytics/?pair=TEST&exclude=TTZ2022,TTZ2023,TTZ2024&years=2").json()
        self.assertEqual(body["used"], ["TTZ2025"])
        self.assertIn("мало истории", body["note"])
        self.assertTrue(next(line for line in body["lines"] if line["code"] == "TTZ2022")["excluded"])

    def test_cleaned_series_and_cache(self):
        raw = self.client.get("/api/analytics/?pair=TEST&hold=5").json()
        rate = self.client.get("/api/analytics/?pair=TEST&hold=5&series=rate").json()
        self.assertAlmostEqual(rate["lines"][0]["y"][-1], raw["lines"][0]["y"][-1] / 2, places=4)
        self.assertEqual(len(views._cache), 2)
        self.client.get("/api/analytics/?pair=TEST&hold=5")
        self.assertEqual(len(views._cache), 2)


class ImportTvTests(TestCase):
    def test_half_hour_bars_become_hours_and_old_quarter_is_created(self):
        pair = Pair.objects.get(code="CNY")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "MOEX_DLY_CRZ2023, 30.csv"
            start = int(datetime(2023, 11, 1, 7, 0, tzinfo=UTC).timestamp())
            lines = ["time,open,high,low,close,Volume"]
            for index, (o, h, l, c) in enumerate([(12.0, 12.2, 11.9, 12.1), (12.1, 12.5, 12.0, 12.4), (12.4, 12.4, 12.3, 12.3)]):
                lines.append(f"{start + index * 1800},{o},{h},{l},{c},10")
            path.write_text("\n".join(lines), encoding="utf-8")
            call_command("import_tv", folder, stdout=StringIO())
        instrument = Instrument.objects.get(code="CRZ2023")
        self.assertEqual((instrument.pair, instrument.finam_symbol), (pair, "CRZ3@RTSX"))
        candles = list(Candle.objects.filter(instrument=instrument).order_by("ts").values_list("open", "high", "low", "close", "volume"))
        self.assertEqual([tuple(float(v) for v in row) for row in candles], [(12.0, 12.5, 11.9, 12.4, 20.0), (12.4, 12.4, 12.3, 12.3, 10.0)])


def write_csv(folder, name, start, closes):
    lines = ["time,open,high,low,close"]
    stamp = int(start.timestamp())
    for index, close in enumerate(closes):
        lines.append(f"{stamp + index * 3600},{close},{close},{close},{close}")
    path = Path(folder) / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class ImportTvMonthlyTests(TestCase):
    def setUp(self):
        self.calendar = Pair.objects.create(code="NGCAL", title="NG", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="LISTED", lot=100)
        self.gas = Pair.objects.create(code="NGNYMEX", title="NG − NYMEX", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="NGNYM", lot=100)
        self.brent = Pair.objects.create(code="BRNYMEX", title="BR − BZ", future_root="BR", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="NYMEX", lot=1000)

    def test_gas_files_go_to_exchange_by_name(self):
        start = datetime(2021, 1, 11, 9, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as folder:
            write_csv(folder, "MOEX_DLY_NGF2021, 60.csv", start, [2.5, 2.6])
            write_csv(folder, "NYMEX_DL_NGG2021, 60.csv", start, [2.4, 2.5])
            write_csv(folder, "NYMEX_DL_BZH2021, 60.csv", start, [55, 56])
            out = StringIO()
            call_command("import_tv", folder, "--dry-run", stdout=out)
            self.assertIn("NGF2021 (будет заведён у NGNYMEX)", out.getvalue())
            self.assertIn("NGF2021-NYM (будет заведён у NGNYMEX)", out.getvalue())
            self.assertFalse(Instrument.objects.filter(code__startswith="NGF2021").exists())
            call_command("import_tv", folder, stdout=StringIO())
        moex = Instrument.objects.get(code="NGF2021")
        nymex = Instrument.objects.get(code="NGF2021-NYM")
        self.assertEqual((moex.finam_symbol, moex.pair.code, moex.kind), ("NGF1@RTSX", "NGNYMEX", "foreign"))
        self.assertEqual(moex.roll_at, nymex.roll_at)
        self.assertEqual((nymex.finam_symbol, nymex.pair.code), ("NGG21@XNYM", "NGNYMEX"))
        self.assertEqual(Candle.objects.filter(instrument=moex).count(), 2)
        self.assertEqual(Candle.objects.filter(instrument=nymex).count(), 2)
        bz = Instrument.objects.get(code="BZH2021")
        self.assertEqual((bz.pair.code, bz.kind), ("BRNYMEX", "foreign"))
        self.assertTrue(Instrument.objects.filter(code="BRG2021", pair=self.brent).exists())


class ExtendedHistoryTests(TestCase):
    """Месячный контракт: история за два месяца до склейки, а не только своё окно."""

    def test_monthly_contract_history_starts_two_months_before_roll(self):
        from market import brent
        from strategy import rebuild
        pair = Pair.objects.create(code="BRNYMEX", title="BR − BZ", future_root="BR", perp_code="", perp_symbol="",
                                   spot_code="", spot_symbol="", foreign_source="NYMEX", lot=1000)
        for row in brent.windows(datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 12, 10, tzinfo=UTC)):
            brent.ensure_window(pair, row)
        moex, nymex = Instrument.objects.get(code="BRF2027"), Instrument.objects.get(code="BZG2027")
        self.assertEqual(moex.roll_at, datetime(2026, 12, 30, 20, 50, tzinfo=UTC))
        day = datetime(2026, 10, 20, tzinfo=UTC)
        candles = []
        while day < datetime(2026, 12, 30, tzinfo=UTC):
            if day.weekday() < 5:
                for hour in (8, 10, 12):
                    ts = day.replace(hour=hour)
                    candles += [Candle(instrument=moex, ts=ts, open=70, high=70, low=70, close=70, volume=1),
                                Candle(instrument=nymex, ts=ts, open=69.5, high=69.5, low=69.5, close=69.5, volume=1)]
            day += timedelta(days=1)
        Candle.objects.bulk_create(candles)
        rebuild.rebuild_spread("BRNYMEX")
        # В базе — только окно: с 30.11.
        first = SpreadBar.objects.filter(pair=pair).order_by("ts").first()
        self.assertGreaterEqual(first.ts, datetime(2026, 11, 30, 20, 50, tzinfo=UTC))
        found = {item["code"]: item for item in contracts(pair, now=datetime(2027, 1, 10, tzinfo=UTC))}
        points = found["BRF2027"]["points"]
        # История с 30.10 20:50 (склейка − 61 день): первый полный день — понедельник 2.11, до склейки 58 дней.
        self.assertEqual(points[-1][0], 58)
        self.assertAlmostEqual(points[0][1], 0.5, places=6)
        self.assertGreater(len(points), 40)
        # История лежит в своей таблице: у часа из окна BRZ2026 есть и бар BRF2027.
        from analytics.models import HistoryBar
        ts = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        codes = set(HistoryBar.objects.filter(pair=pair, ts=ts).values_list("contract__code", flat=True))
        self.assertEqual(codes, {"BRF2027"})
        self.assertEqual(SpreadBar.objects.filter(pair=pair, ts=ts).count(), 0)


class FixQuartersTests(TestCase):
    def setUp(self):
        self.usd = Pair.objects.create(
            code="USD", title="доллар", future_root="Si", perp_code="USDRUBF", perp_symbol="USDRUBF@RTSX",
            spot_code="USDRUB_TOM", spot_symbol="", foreign_source="DFEDTARU", future_scale=1000, lot=1000)
        self.perp = Instrument.objects.create(pair=self.usd, code="USDRUBF", finam_symbol="USDRUBF@RTSX", kind="perp")
        self.recent = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
        self.old = datetime(2023, 11, 1, 10, 0, tzinfo=UTC)
        price(self.perp, self.recent, 83)
        front = Instrument.objects.create(pair=self.usd, code="SiZ2026", finam_symbol="SiZ6@RTSX", kind="future")
        price(front, self.recent, 83500)

    def test_perp_imported_x1000_is_divided_and_quarters_kept(self):
        """Как RUS_USDRUBF_1000 из TradingView: старые часы вечного ×1000, кварталы верные."""
        other = Pair.objects.create(code="SIX", title="чужая", future_root="Si", perp_code="", perp_symbol="",
                                    spot_code="", spot_symbol="", foreign_source="NONE", future_scale=1)
        quarter = Instrument.objects.create(pair=other, code="SiZ2023", finam_symbol="", kind="future")
        price(self.perp, self.old, 90000)
        price(quarter, self.old, 91000)
        out = StringIO()
        call_command("fix_quarters", "--dry-run", stdout=out)
        self.assertIn("SiZ2023: у пары SIX", out.getvalue())
        self.assertIn("USDRUBF (масштаб 1): из 2 свечей 1 ×1000", out.getvalue())
        self.assertNotIn("SiZ2026 (", out.getvalue())
        call_command("fix_quarters", stdout=StringIO())
        quarter.refresh_from_db()
        self.assertEqual((quarter.pair, quarter.finam_symbol), (self.usd, "SiZ3@RTSX"))
        self.assertEqual(quarter.roll_at.date(), datetime(2023, 12, 21).date())
        self.assertEqual(float(Candle.objects.get(instrument=quarter).close), 91000.0)
        self.assertEqual(float(Candle.objects.get(instrument=self.perp, ts=self.old).close), 90.0)
        self.assertEqual(float(Candle.objects.get(instrument=self.perp, ts=self.recent).close), 83.0)

    def test_quarters_multiplied_by_mistake_are_restored(self):
        quarter = Instrument.objects.create(pair=self.usd, code="SiZ2023", finam_symbol="SiZ3@RTSX", kind="future")
        price(quarter, self.old, 91000000)
        call_command("fix_quarters", stdout=StringIO())
        self.assertEqual(float(Candle.objects.get(instrument=quarter).close), 91000.0)

    def test_import_tv_divides_file_in_thousands(self):
        with tempfile.TemporaryDirectory() as folder:
            write_csv(folder, "RUS_USDRUBF_1000, 60.csv", datetime(2024, 3, 1, 9, 0, tzinfo=UTC), [92000, 92100])
            out = StringIO()
            call_command("import_tv", folder, stdout=out)
        self.assertIn("цены ÷1000", out.getvalue())
        self.assertEqual(float(Candle.objects.filter(instrument=self.perp).order_by("ts").first().close), 92.0)

    def test_import_owner_is_pair_that_already_holds_quarters(self):
        from market.management.commands.import_tv import quarter_owner
        Pair.objects.create(code="AAA", title="чужая", future_root="Si", perp_code="", perp_symbol="",
                            spot_code="", spot_symbol="", foreign_source="NONE", future_scale=1)
        self.assertEqual(quarter_owner("SI"), self.usd)
        Instrument.objects.filter(code="SiZ2026").delete()
        Candle.objects.all().delete()
        self.assertEqual(quarter_owner("si"), self.usd)

    def test_import_tv_creates_gold_quarter_at_triangle(self):
        from market.management.commands.import_tv import quarter_owner
        trigd = Pair.objects.create(code="TRIGD", title="GD + SI − GLDRUBF", future_root="GD", perp_code="GLDRUBF",
                                    perp_symbol="GLDRUBF@RTSX", spot_code="SI", spot_symbol="", foreign_source="TRI",
                                    lot=1, future_scale=1, mic="RTSX")
        Pair.objects.create(code="TRIGDS", title="секонды", future_root="GD", perp_code="", perp_symbol="",
                            spot_code="", spot_symbol="", foreign_source="TRI", lot=1, future_scale=1)
        self.assertEqual(quarter_owner("GD"), trigd)
        with tempfile.TemporaryDirectory() as folder:
            write_csv(folder, "RUS_GDH2025, 60.csv", datetime(2025, 1, 10, 9, 0, tzinfo=UTC), [2650, 2660])
            out = StringIO()
            call_command("import_tv", folder, stdout=out)
        quarter = Instrument.objects.get(code="GDH2025")
        self.assertEqual((quarter.pair, quarter.finam_symbol, quarter.roll_at.date()),
                         (trigd, "GDH5@RTSX", datetime(2025, 3, 20).date()))
        self.assertEqual(Candle.objects.filter(instrument=quarter).count(), 2)


def price(instrument, ts, value):
    Candle.objects.create(instrument=instrument, ts=ts, open=value, high=value, low=value, close=value, volume=1)
