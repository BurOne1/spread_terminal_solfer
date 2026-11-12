"""Тесты движка, формул и пересчёта spread_rate.

python manage.py test --settings=config.settings_test
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from market.models import Candle, Instrument, Pair
from rates.models import RateDay
from strategy import rebuild
from strategy.engine import run
from strategy.indicators import fair_spread
from strategy.models import SpreadBar
from strategy.runner import entry_rules, priced

UTC = timezone.utc
BASE = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # понедельник
LEVELS = {"long_pcts": (-0.01,), "short_pcts": (0.01,), "long_tp": (0.0025,), "short_tp": (-0.0025,)}


def bars(rates, contracts=None):
    """Бары движка: спот 100, raw = rate, fair 0."""
    rows = []
    for index, rate in enumerate(rates):
        rows.append({
            "ts": BASE + timedelta(hours=index),
            "contract": contracts[index] if contracts else "A",
            "raw": rate,
            "rate": rate,
            "spot": 100.0,
            "fair": 0.0,
            "future": 100.0 + rate,
            "perp": 100.0,
        })
    return rows


class EngineTests(SimpleTestCase):
    def test_short_enters_on_level_and_exits_on_take(self):
        _points, trades, opened = run(bars([0.0, 0.5, 1.2, 0.3, -0.3]), [], **LEVELS)
        self.assertIsNone(opened)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual((trade["side"], trade["reason"], trade["n"]), ("SHORT", "tp", 1))
        self.assertEqual(trade["entry"], BASE + timedelta(hours=2))
        self.assertEqual(trade["exit"], BASE + timedelta(hours=4))
        self.assertAlmostEqual(trade["price"], 1500.0)
        self.assertAlmostEqual(trade["entry_spread"], 1.2)
        self.assertAlmostEqual(trade["exit_spread"], -0.3)

    def test_last_bar_keeps_position_open(self):
        points, trades, opened = run(bars([0.0, 0.5, 1.2, 1.1]), [], **LEVELS)
        self.assertEqual(trades, [])
        self.assertEqual((opened["side"], opened["n"]), ("SHORT", 1))
        self.assertAlmostEqual(opened["price"], 100.0)
        self.assertEqual(points[-1]["marker"], "S")

    def test_roll_closes_before_quiet_zone(self):
        rates = [0.0, 0.5, 1.2] + [1.1] * 47
        contracts = ["A"] * 40 + ["B"] * 10
        _points, trades, opened = run(bars(rates, contracts), [], **LEVELS)
        self.assertIsNone(opened)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "roll")
        self.assertEqual(trades[0]["exit"], BASE + timedelta(hours=40 - 26))

    def test_fair_spread(self):
        self.assertAlmostEqual(fair_spread(12.0, 16.0, 1.5, 73), 12.0 * 0.145 * 0.2)


class PricedTests(SimpleTestCase):
    def bar(self, raw_open):
        return SimpleNamespace(
            raw_spread=Decimal("0.3"), spread_rate=Decimal("0.1"),
            raw_open=raw_open, rate_open=None if raw_open is None else Decimal("-0.1"),
        )

    def test_bases(self):
        bar = self.bar(Decimal("0.2"))
        self.assertEqual(priced(bar, "close"), (0.3, 0.1))
        self.assertEqual(priced(bar, "open"), (0.2, -0.1))
        raw, rate = priced(bar, "mid")
        self.assertAlmostEqual(raw, 0.25)
        self.assertAlmostEqual(rate, 0.0)

    def test_old_bar_without_open_falls_back_to_close(self):
        self.assertEqual(priced(self.bar(None), "mid"), (0.3, 0.1))


def make_cny(hours=5):
    """CNY: вечный, спот, CRZ2026 и часы с BASE. future = perp + 0.3, на открытии + 0.2."""
    pair = Pair.objects.get(code="CNY")
    perp = Instrument.objects.create(pair=pair, code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")
    spot = Instrument.objects.create(pair=pair, code="CNYRUB_TOM", finam_symbol="CNYRUB_TOM@MISX", kind="spot")
    future = Instrument.objects.create(
        pair=pair, code="CRZ2026", finam_symbol="CRZ6@RTSX", kind="future",
        listed_at=datetime(2026, 9, 17, 20, 50, tzinfo=UTC),
        roll_at=datetime(2026, 12, 17, 20, 50, tzinfo=UTC),
    )
    RateDay.objects.create(date=BASE.date() - timedelta(days=4), ruonia=Decimal("16"), shibor=Decimal("1.5"))
    for index in range(hours):
        ts = BASE + timedelta(hours=index)
        opened = Decimal("11.90") + Decimal("0.01") * index
        closed = Decimal("12.00") + Decimal("0.01") * index
        candle(perp, ts, opened, closed)
        candle(spot, ts, Decimal("11.90"), Decimal("11.95"))
        candle(future, ts, opened + Decimal("0.2"), closed + Decimal("0.3"))
    return pair, future


def candle(instrument, ts, opened, closed):
    Candle.objects.update_or_create(
        instrument=instrument, ts=ts,
        defaults={"open": opened, "high": max(opened, closed), "low": min(opened, closed), "close": closed},
    )


class RebuildTests(TestCase):
    def setUp(self):
        self.pair, self.future = make_cny()

    def test_close_and_open_variants(self):
        self.assertEqual(rebuild.rebuild_spread("CNY"), 5)
        bar = SpreadBar.objects.get(pair=self.pair, ts=BASE)
        days = (date_of(self.future.roll_at) - BASE.date()).days
        self.assertEqual(bar.days_left, days)
        self.assertAlmostEqual(float(bar.raw_spread), 0.3, places=6)
        self.assertAlmostEqual(float(bar.raw_open), 0.2, places=6)
        self.assertAlmostEqual(float(bar.spread_rate), 0.3 - fair_spread(11.95, 16, 1.5, days), places=5)
        self.assertAlmostEqual(float(bar.rate_open), 0.2 - fair_spread(11.90, 16, 1.5, days), places=5)

    def test_tail_rebuild_keeps_older_bars(self):
        rebuild.rebuild_spread("CNY")
        first = SpreadBar.objects.get(pair=self.pair, ts=BASE)
        moved = BASE + timedelta(hours=3)
        perp_close = Decimal("12.03")
        candle(self.future, moved, Decimal("12.13"), perp_close + Decimal("0.5"))
        self.assertEqual(rebuild.rebuild_spread("CNY", since=moved), 2)
        self.assertEqual(SpreadBar.objects.filter(pair=self.pair).count(), 5)
        self.assertEqual(SpreadBar.objects.get(pair=self.pair, ts=BASE).id, first.id)
        self.assertAlmostEqual(float(SpreadBar.objects.get(pair=self.pair, ts=moved).raw_spread), 0.5, places=6)

    def test_flush_rebuilds_marked_pair_and_its_triangles(self):
        rebuild._dirty.clear()
        rebuild.mark_dirty("CNY", BASE + timedelta(hours=2))
        rebuild.mark_dirty("CNY", BASE + timedelta(hours=1))
        self.assertEqual(rebuild.flush_dirty(), {"CNY": 4})
        self.assertEqual(rebuild.flush_dirty(), {})
        rebuild.mark_dirty("USD", BASE)
        self.assertEqual(set(rebuild.flush_dirty()), {"USD", "TRIGD", "TRISV", "TRIGL"})


def date_of(ts):
    return ts.astimezone(UTC).date()


# Склейка SiZ2026 — третий четверг декабря, 17.12.2026 20:50 UTC.
BEFORE_ROLL = datetime(2026, 12, 16, 10, 0, tzinfo=UTC)
AFTER_ROLL = datetime(2026, 12, 18, 10, 0, tzinfo=UTC)


def make_quarters(pair, root, prices):
    """Кварталы U2026…M2027 с окнами склеек. prices: {буква и год: [(час, open, close)]}."""
    from market.roll import quarter_points
    rows = quarter_points(root, datetime(2026, 7, 1, tzinfo=UTC), datetime(2027, 4, 1, tzinfo=UTC))
    found = {}
    for row in rows:
        found[row["code"]] = Instrument.objects.create(
            pair=pair, code=row["code"], finam_symbol=row["secid"] + "@RTSX", kind="future",
            listed_at=row["listed_at"], roll_at=row["roll_at"],
        )
    for code, items in prices.items():
        for ts, opened, closed in items:
            candle(found[root + code], ts, Decimal(opened), Decimal(closed))
    return found


def make_usd():
    """USD с кварталами Si и календарь SICAL. Секонд − фронт: 2.3 до склейки, 2.2 после."""
    usd = Pair.objects.create(
        code="USD", title="доллар", future_root="Si", perp_code="USDRUBF", perp_symbol="USDRUBF@RTSX",
        spot_code="USDRUB_TOM", spot_symbol="USD000UTSTOM@MISX", foreign_source="DFEDTARU", future_scale=1000,
    )
    spot = Instrument.objects.create(pair=usd, code="USDRUB_TOM", finam_symbol="USD000UTSTOM@MISX", kind="spot")
    for ts in (BEFORE_ROLL, AFTER_ROLL):
        candle(spot, ts, Decimal("94"), Decimal("95"))
    RateDay.objects.create(date=date_of(BEFORE_ROLL) - timedelta(days=3), ruonia=Decimal("16"), dfedtaru=Decimal("4"))
    quarters = make_quarters(usd, "Si", {
        "Z2026": [(BEFORE_ROLL, "95000", "95500")],
        "H2027": [(BEFORE_ROLL, "97000", "97800"), (AFTER_ROLL, "97100", "97900")],
        "M2027": [(AFTER_ROLL, "99000", "100100")],
    })
    calendar = Pair.objects.create(
        code="SICAL", title="Si: секонд − фронт", base="USD", future_root="Si", perp_code="", perp_symbol="",
        spot_code="USDRUB_TOM", spot_symbol="", foreign_source="CAL", future_scale=1000,
    )
    return usd, calendar, quarters


class CalendarTests(TestCase):
    def setUp(self):
        rebuild._dirty.clear()
        self.usd, self.pair, self.quarters = make_usd()

    def test_second_minus_front_and_switch_after_roll(self):
        self.assertEqual(rebuild.rebuild_spread("SICAL"), 2)
        before = SpreadBar.objects.get(pair=self.pair, ts=BEFORE_ROLL)
        after = SpreadBar.objects.get(pair=self.pair, ts=AFTER_ROLL)
        self.assertEqual(before.contract.code, "SiZ2026")
        self.assertEqual(after.contract.code, "SiH2027")
        self.assertAlmostEqual(float(before.raw_spread), 2.3, places=6)
        self.assertAlmostEqual(float(before.raw_open), 2.0, places=6)
        self.assertAlmostEqual(float(after.raw_spread), 2.2, places=6)
        self.assertEqual(before.days_left, 1)
        tenor = (date_of(self.quarters["SiH2027"].roll_at) - date_of(self.quarters["SiZ2026"].roll_at)).days
        self.assertEqual(tenor, 91)
        self.assertAlmostEqual(float(before.fair), 95 * (16 - 4) / 100 * tenor / 365, places=5)
        self.assertAlmostEqual(float(before.spread_rate), 2.3 - float(before.fair), places=5)
        self.assertAlmostEqual(float(before.rate_open), 2.0 - 94 * 0.12 * tenor / 365, places=5)

    def test_tail_and_flush_follow_base(self):
        rebuild.rebuild_spread("SICAL")
        candle(self.quarters["SiM2027"], AFTER_ROLL, Decimal("99000"), Decimal("100300"))
        rebuild.mark_dirty("USD", AFTER_ROLL)
        done = rebuild.flush_dirty()
        self.assertEqual(done["SICAL"], 1)
        self.assertEqual(SpreadBar.objects.filter(pair=self.pair).count(), 2)
        self.assertAlmostEqual(float(SpreadBar.objects.get(pair=self.pair, ts=AFTER_ROLL).raw_spread), 2.4, places=6)

    def test_strategy_runs_without_funding(self):
        from strategy.runner import funding_of, pass_pair
        rebuild.rebuild_spread("SICAL")
        self.assertEqual(funding_of(self.pair), ([], "daily"))
        points, _trades, _opened = pass_pair(self.pair)
        self.assertEqual([point["contract"] for point in points], ["SiZ2026", "SiH2027"])


class TriangleSecondsTests(TestCase):
    def test_gold_triangle_on_seconds(self):
        make_usd()
        gold = Pair.objects.create(
            code="GOLD", title="золото", future_root="GL", perp_code="GLDRUBF", perp_symbol="GLDRUBF@RTSX",
            spot_code="GLDRUBF", spot_symbol="GLDRUBF@RTSX", foreign_source="NONE", lot=1,
        )
        perp = Instrument.objects.create(pair=gold, code="GLDRUBF", finam_symbol="GLDRUBF@RTSX", kind="perp")
        candle(perp, BEFORE_ROLL, Decimal("9000"), Decimal("9100"))
        tri = Pair.objects.create(
            code="TRIGD", title="GD + SI − GLDRUBF", future_root="GD", perp_code="GLDRUBF", perp_symbol="",
            spot_code="SI", spot_symbol="", foreign_source="TRI", lot=1,
        )
        make_quarters(tri, "GD", {
            "Z2026": [(BEFORE_ROLL, "2900", "2950")],
            "H2027": [(BEFORE_ROLL, "2930", "2990")],
        })
        seconds = Pair.objects.create(
            code="TRIGD2", title="GD2 + SI2 − GLDRUBF", base="TRIGD", future_root="GD", perp_code="GLDRUBF",
            perp_symbol="", spot_code="SI", spot_symbol="", foreign_source="TRI", lot=1,
        )
        self.assertEqual(rebuild.rebuild_spread("TRIGD2"), 1)
        bar = SpreadBar.objects.get(pair=seconds, ts=BEFORE_ROLL)
        self.assertEqual(bar.contract.code, "GDZ2026")
        self.assertEqual(bar.days_left, 1)
        days = (datetime(2027, 3, 18).date() - date_of(BEFORE_ROLL)).days
        raw, fair, cleaned = rebuild._tri_values(2990, 97.8, 9100, 16.0, 4.0, days)
        self.assertAlmostEqual(float(bar.raw_spread), raw, places=5)
        self.assertAlmostEqual(float(bar.fair), fair, places=5)
        self.assertAlmostEqual(float(bar.spread_rate), cleaned, places=5)
        rebuild.rebuild_spread("TRIGD")
        front = SpreadBar.objects.get(pair=tri, ts=BEFORE_ROLL)
        self.assertAlmostEqual(float(front.raw_spread), rebuild._tri_values(2950, 95.5, 9100, 16.0, 4.0, 1)[0], places=5)
        rebuild._dirty.clear()
        rebuild.mark_dirty("USD", BEFORE_ROLL)
        self.assertIn("TRIGD2", rebuild.flush_dirty())


# BZF2027 истекает 30.11.2026 (понедельник), окно BRZ2026/BZF2027 до 20:50 UTC.
BRENT_FRONT = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
BRENT_LAST = datetime(2026, 11, 30, 10, 0, tzinfo=UTC)
BRENT_NEXT = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)


def make_brent():
    """BRNYMEX с окнами ноября и декабря. raw: 0.12, 0.05 и −0.03 в следующем окне."""
    from market.brent import ensure_window, windows
    pair = Pair.objects.create(
        code="BRNYMEX", title="BR MOEX − BZ NYMEX", future_root="BR", perp_code="", perp_symbol="",
        spot_code="", spot_symbol="", foreign_source="NYMEX", quote="USD",
    )
    legs = {}
    for row in windows(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 5, tzinfo=UTC)):
        ensure_window(pair, row)
        legs[row["code"]] = Instrument.objects.get(code=row["code"])
        legs[row["nymex_code"]] = Instrument.objects.get(code=row["nymex_code"])
    prices = {
        "BRZ2026": [(BRENT_FRONT, "70.10", "70.32"), (BRENT_FRONT + timedelta(hours=1), "70.30", "70.40"),
                    (datetime(2026, 11, 28, 10, 0, tzinfo=UTC), "70", "70"), (BRENT_LAST, "71", "71.05"),
                    (BRENT_NEXT, "71", "71")],
        "BZF2027": [(BRENT_FRONT, "70.00", "70.20"), (datetime(2026, 11, 28, 10, 0, tzinfo=UTC), "69", "69"),
                    (BRENT_LAST, "71", "71.00")],
        "BRF2027": [(BRENT_NEXT, "72.00", "71.97")],
        "BZG2027": [(BRENT_NEXT, "72.00", "72.00")],
    }
    for code, items in prices.items():
        for ts, opened, closed in items:
            candle(legs[code], ts, Decimal(opened), Decimal(closed))
    return pair, legs


class BrentTests(TestCase):
    def setUp(self):
        self.pair, self.legs = make_brent()
        rebuild.rebuild_spread("BRNYMEX")

    def test_only_hours_where_both_legs_trade(self):
        bars = list(SpreadBar.objects.filter(pair=self.pair).order_by("ts"))
        # Час только с BR, суббота и BR после экспирации BZ пропущены.
        self.assertEqual([bar.ts for bar in bars], [BRENT_FRONT, BRENT_LAST, BRENT_NEXT])
        first = bars[0]
        self.assertAlmostEqual(float(first.raw_spread), 0.12, places=6)
        self.assertAlmostEqual(float(first.raw_open), 0.10, places=6)
        self.assertAlmostEqual(float(first.spread_rate), 0.12, places=6)
        self.assertEqual(float(first.fair), 0.0)
        self.assertAlmostEqual(float(first.spot), 70.20, places=6)
        self.assertEqual(first.days_left, 7)

    def test_switches_to_next_month_after_bz_expiry(self):
        last, nxt = SpreadBar.objects.filter(pair=self.pair, ts__in=[BRENT_LAST, BRENT_NEXT]).order_by("ts")
        self.assertEqual((last.contract.code, last.days_left), ("BRZ2026", 1))
        self.assertEqual(nxt.contract.code, "BRF2027")
        self.assertAlmostEqual(float(nxt.raw_spread), -0.03, places=6)
        self.assertEqual(nxt.days_left, 29)  # до 30.12: 31.12 Мосбиржа не работает

    def test_tail_rebuild_matches_full(self):
        full = list(SpreadBar.objects.filter(pair=self.pair).order_by("ts").values_list("ts", "raw_spread"))
        rebuild.rebuild_spread("BRNYMEX", since=BRENT_LAST)
        tail = list(SpreadBar.objects.filter(pair=self.pair).order_by("ts").values_list("ts", "raw_spread"))
        self.assertEqual(full, tail)

    def test_long_enters_without_sign_rule(self):
        # fair = 0, знаки rate и raw равны: общее правило лонг бы не пустило.
        rates = [0.0, -0.5, -1.2, -0.5, 0.3]
        _points, blocked, _opened = run(bars(rates), [], **LEVELS)
        self.assertEqual(blocked, [])
        _points, trades, _opened = run(bars(rates), [], long_needs_sign=False, min_spread=0.0, **LEVELS)
        self.assertEqual([trade["side"] for trade in trades], ["LONG"])
        self.assertEqual(entry_rules(self.pair), {"long_needs_sign": False, "min_spread": 0.0})


# CNHZ26 и 6EZ26 истекают 14.12.2026 (понедельник, за два рабочих дня до третьей среды).
CROSS_TS = datetime(2026, 12, 10, 10, 0, tzinfo=UTC)  # четверг
CROSS_SATURDAY = datetime(2026, 12, 12, 10, 0, tzinfo=UTC)


def make_cross():
    """CRCNH с ногами Z2026: CR 12.4, CNH 6.7, Si 82 900 → raw 12.4×6.7×1000 − 82 900 = 180."""
    from market import cross
    usd = Pair.objects.create(code="USD", title="доллар", future_root="Si", perp_code="USDRUBF", perp_symbol="", spot_code="", spot_symbol="", foreign_source="DFEDTARU")
    cny = Pair.objects.get(code="CNY")
    si = Instrument.objects.create(pair=usd, code="SiZ2026", finam_symbol="SiZ6@RTSX", kind="future")
    cr = Instrument.objects.create(pair=cny, code="CRZ2026.T", finam_symbol="CRZ6@RTSX", kind="future")
    pair = Pair.objects.create(code="CRCNH", title="CR × CNH − Si", future_root="CR", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CMEFX", lot=1)
    cross.ensure(pair, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 20, tzinfo=UTC))
    cnh = Instrument.objects.get(code="CNHZ2026")
    for ts in (CROSS_TS, CROSS_SATURDAY):
        candle(cr, ts, Decimal("12.40"), Decimal("12.40"))
        candle(cnh, ts, Decimal("6.70"), Decimal("6.70"))
        candle(si, ts, Decimal("82800"), Decimal("82900"))
    candle(cnh, CROSS_TS + timedelta(hours=1), Decimal("6.71"), Decimal("6.71"))
    return pair, cnh


class CrossTests(TestCase):
    def test_cross_rate_minus_moex_leg_in_points(self):
        pair, cnh = make_cross()
        self.assertEqual(cnh.roll_at, datetime(2026, 12, 14, 14, 0, tzinfo=UTC))
        self.assertEqual(rebuild.rebuild_spread("CRCNH"), 1)
        bar = SpreadBar.objects.get(pair=pair)
        # Суббота и час только с CNH пропущены.
        self.assertEqual(bar.ts, CROSS_TS)
        self.assertAlmostEqual(float(bar.raw_spread), 180.0, places=4)
        self.assertAlmostEqual(float(bar.raw_open), 280.0, places=4)
        self.assertEqual((float(bar.fair), float(bar.spot)), (0.0, 82900.0))
        self.assertEqual(bar.days_left, 4)
        self.assertEqual(entry_rules(pair), {"long_needs_sign": False, "min_spread": 0.0})

    def test_moex_leg_rebuilds_cross(self):
        make_cross()
        rebuild._dirty.clear()
        rebuild.mark_dirty("USD", CROSS_TS)
        self.assertIn("CRCNH", rebuild.flush_dirty())


NG_TS = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


class ListedSpreadTests(TestCase):
    def test_ng_calendar_is_one_quote_with_absolute_levels(self):
        from market import ngcal
        from charts.views import _levels
        pair = Pair.objects.create(code="NGCAL", title="NG", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="LISTED", lot=100, quote="USD")
        ngcal.ensure(pair, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
        spread = Instrument.objects.get(code="NGX6NGZ6")
        self.assertEqual(spread.finam_symbol, "NGX6NGZ6@RTSX")
        # Ног календарь не заводит: цена инструмента и есть спред.
        self.assertFalse(Instrument.objects.filter(code="NGX2026").exists())
        candle(spread, NG_TS - timedelta(hours=1), Decimal("-0.30"), Decimal("-0.31"))
        candle(spread, NG_TS, Decimal("-0.31"), Decimal("-0.35"))
        self.assertEqual(rebuild.rebuild_spread("NGCAL"), 2)
        bars = list(SpreadBar.objects.filter(pair=pair).order_by("ts"))
        self.assertAlmostEqual(float(bars[1].raw_spread), -0.35, places=6)
        self.assertAlmostEqual(float(bars[1].raw_open), -0.31, places=6)
        self.assertEqual((float(bars[0].spot), float(bars[0].fair)), (1.0, 0.0))
        # Уровни абсолютные: 0.07 — это 0.07 $/MMBtu, а не 7 % цены.
        Pair.objects.filter(pk=pair.pk).update(short_levels={"signed": True, "values": [0.07]})
        pair.refresh_from_db()
        payload = _levels(pair)
        self.assertTrue(payload["absolute"])
        self.assertAlmostEqual(payload["short"][0], 0.07, places=9)


class TriangleSpotTests(TestCase):
    def test_spot_gold_instead_of_perpetual(self):
        make_usd()
        gold = Pair.objects.create(
            code="GOLD", title="золото", future_root="GL", perp_code="GLDRUBF", perp_symbol="GLDRUBF@RTSX",
            spot_code="GLDRUB_TOM", spot_symbol="GLDRUB_TOM@MISX", foreign_source="NONE", lot=1,
        )
        perp = Instrument.objects.create(pair=gold, code="GLDRUBF", finam_symbol="GLDRUBF@RTSX", kind="perp")
        spot = Instrument.objects.create(pair=gold, code="GLDRUB_TOM", finam_symbol="GLDRUB_TOM@MISX", kind="spot")
        candle(perp, BEFORE_ROLL, Decimal("9000"), Decimal("9100"))
        candle(spot, BEFORE_ROLL, Decimal("9050"), Decimal("9080"))
        tri = Pair.objects.create(
            code="TRIGD", title="GD + SI − GLDRUBF", future_root="GD", perp_code="GLDRUBF", perp_symbol="",
            spot_code="SI", spot_symbol="", foreign_source="TRI", lot=1,
        )
        make_quarters(tri, "GD", {"Z2026": [(BEFORE_ROLL, "2900", "2950")]})
        spot_tri = Pair.objects.create(
            code="TRIGDS", title="GD + SI − GLDRUB_TOM", future_root="GD", perp_code="GLDRUB_TOM", perp_symbol="",
            spot_code="SI", spot_symbol="", foreign_source="TRI", lot=1,
        )
        self.assertEqual(rebuild.rebuild_spread("TRIGDS"), 1)
        rebuild.rebuild_spread("TRIGD")
        on_spot = SpreadBar.objects.get(pair=spot_tri)
        on_perp = SpreadBar.objects.get(pair=tri)
        self.assertAlmostEqual(float(on_spot.raw_spread), rebuild._tri_values(2950, 95.5, 9080, 16.0, 4.0, 1)[0], places=5)
        self.assertAlmostEqual(float(on_perp.raw_spread), rebuild._tri_values(2950, 95.5, 9100, 16.0, 4.0, 1)[0], places=5)
        self.assertEqual(on_spot.contract.code, "GDZ2026")
        from strategy.runner import funding_of
        self.assertEqual(funding_of(spot_tri), ([], "daily"))
        self.assertEqual(spot_tri.quarters_code, "TRIGD")
        rebuild._dirty.clear()
        rebuild.mark_dirty("GOLD", BEFORE_ROLL)
        self.assertIn("TRIGDS", rebuild.flush_dirty())


class GasNymexTests(TestCase):
    def test_moex_minus_nymex_next_month_with_own_moex_leg(self):
        from market import ngnymex
        start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC)
        pair = Pair.objects.create(code="NGNYMEX", title="NG MOEX − NG NYMEX", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="NGNYM", lot=100, quote="USD")
        ngnymex.ensure(pair, start, end)
        window = Instrument.objects.get(code="NGX2026-NYM")
        moex = Instrument.objects.get(code="NGX2026")
        # Нога Мосбиржи — своя у NGNYMEX, с тем же окном.
        self.assertEqual((window.finam_symbol, moex.finam_symbol, moex.pair.code, moex.kind),
                         ("NGZ26@XNYM", "NGX6@RTSX", "NGNYMEX", "foreign"))
        self.assertEqual(moex.roll_at, window.roll_at)
        self.assertEqual(window.roll_at, datetime(2026, 11, 30, 20, 50, tzinfo=UTC))
        candle(moex, NG_TS, Decimal("3.40"), Decimal("3.45"))
        candle(window, NG_TS, Decimal("3.38"), Decimal("3.41"))
        candle(window, NG_TS + timedelta(hours=1), Decimal("3.40"), Decimal("3.42"))
        self.assertEqual(rebuild.rebuild_spread("NGNYMEX"), 1)
        bar = SpreadBar.objects.get(pair=pair)
        self.assertAlmostEqual(float(bar.raw_spread), 0.04, places=6)
        self.assertAlmostEqual(float(bar.raw_open), 0.02, places=6)
        self.assertEqual((float(bar.fair), float(bar.spot)), (0.0, 3.41))
        rebuild._dirty.clear()
        rebuild.mark_dirty("NGNYMEX", NG_TS)
        self.assertEqual(rebuild.flush_dirty(), {"NGNYMEX": 1})


class HistoryBarsTests(TestCase):
    """Режим истории аналитики: окна перекрываются, в базу ничего не пишется."""

    def test_quarter_pair_gets_bars_of_next_quarter_before_its_window(self):
        usd, _calendar, quarters = make_usd()
        perp = Instrument.objects.create(pair=usd, code="USDRUBF", finam_symbol="USDRUBF@RTSX", kind="perp")
        candle(perp, BEFORE_ROLL, Decimal("95"), Decimal("95.2"))
        candle(perp, AFTER_ROLL, Decimal("95"), Decimal("95.3"))
        self.assertEqual(rebuild.rebuild_spread("USD"), 2)
        stored = SpreadBar.objects.filter(pair=usd).count()
        rows = rebuild.history_bars(usd)
        self.assertEqual(SpreadBar.objects.filter(pair=usd).count(), stored)
        by_contract = {(row.ts, row.contract.code): row for row in rows}
        # До склейки SiZ2026 есть и бар SiH2027: за два квартала до своей склейки он уже в истории.
        self.assertIn((BEFORE_ROLL, "SiZ2026"), by_contract)
        early = by_contract[(BEFORE_ROLL, "SiH2027")]
        self.assertAlmostEqual(float(early.raw_spread), 97.8 - 95.2, places=6)
        self.assertGreater(early.days_left, 80)
        self.assertEqual(rebuild.history_span(usd), rebuild.HISTORY_QUARTERLY)


class GasLegOwnerTests(TestCase):
    def test_fix_moves_legs_to_gas_pair(self):
        from io import StringIO
        from django.core.management import call_command
        from market import ngcal
        gas = Pair.objects.create(code="NGNYMEX", title="NG − NYMEX", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="NGNYM", lot=100)
        calendar = Pair.objects.create(code="NGCAL", title="NG", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="LISTED", lot=100, quote="USD")
        # Как после старого import_tv: одна нога у календаря, другая у NGNYMEX без окна.
        old = Instrument.objects.create(pair=calendar, code="NGX2026", finam_symbol="NGX6@RTSX", kind="foreign")
        Instrument.objects.create(pair=gas, code="NGF2025", finam_symbol="NGF5@RTSX", kind="foreign")
        ngcal.ensure(calendar, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
        spread = Instrument.objects.get(code="NGX6NGZ6")
        candle(old, NG_TS, Decimal("3.40"), Decimal("3.45"))
        candle(spread, NG_TS, Decimal("-0.31"), Decimal("-0.35"))
        out = StringIO()
        call_command("fix_ng_legs", "--dry-run", stdout=out)
        self.assertIn("у NGCAL: NGX2026", out.getvalue())
        self.assertIn("у NGNYMEX: NGF2025 (без окна)", out.getvalue())
        call_command("fix_ng_legs", stdout=StringIO())
        old.refresh_from_db()
        early = Instrument.objects.get(code="NGF2025")
        self.assertEqual((old.pair, early.pair), (gas, gas))
        self.assertEqual(early.roll_at, datetime(2025, 1, 31, 20, 50, tzinfo=UTC))
        # Календарю ноги не нужны: он строится из своей котировки.
        self.assertEqual(rebuild.rebuild_spread("NGCAL"), 1)
        self.assertAlmostEqual(float(SpreadBar.objects.get(pair=calendar).raw_spread), -0.35, places=6)
