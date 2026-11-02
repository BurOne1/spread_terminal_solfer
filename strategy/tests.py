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
from strategy.runner import priced

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
