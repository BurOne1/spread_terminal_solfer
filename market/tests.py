"""Живая свеча из котировок и хвост догрузки."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from market import live
from market.models import Candle, Instrument, Pair
from strategy import rebuild

UTC = timezone.utc


def frozen(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)
    return patch("market.live.datetime", Frozen)


class SaveQuoteTests(TestCase):
    def setUp(self):
        rebuild._dirty.clear()
        pair = Pair.objects.get(code="CNY")
        self.perp = Instrument.objects.create(pair=pair, code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")

    def tearDown(self):
        rebuild._dirty.clear()

    def test_tick_from_previous_hour_opens_new_hour(self):
        now = datetime(2026, 10, 5, 11, 0, 5, tzinfo=UTC)
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now - timedelta(seconds=15), Decimal("12.10"))
        hour = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
        self.assertIn("новый час", text)
        self.assertEqual(Candle.objects.get(instrument=self.perp).ts, hour)
        self.assertEqual(rebuild._dirty, {"CNY": hour})

    def test_stale_snapshot_is_skipped(self):
        now = datetime(2026, 10, 5, 11, 0, 5, tzinfo=UTC)
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now - timedelta(hours=3), Decimal("12.10"))
        self.assertIn("старая котировка", text)
        self.assertFalse(Candle.objects.exists())
        self.assertEqual(rebuild._dirty, {})

    def test_weekend_is_skipped(self):
        now = datetime(2026, 10, 10, 11, 0, 5, tzinfo=UTC)  # суббота
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now, Decimal("12.10"))
        self.assertIn("биржа закрыта", text)
        self.assertFalse(Candle.objects.exists())


class GapWindowTests(SimpleTestCase):
    def test_last_hour_is_always_fetched_again(self):
        now = datetime(2026, 10, 5, 11, 20, tzinfo=UTC)
        last = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
        step = timedelta(hours=1)
        self.assertEqual(live.gap_windows([last], step, now, timedelta(days=30)), [(last - step, now)])

    def test_empty_base_takes_full_depth(self):
        now = datetime(2026, 10, 5, 11, 20, tzinfo=UTC)
        depth = timedelta(days=30)
        self.assertEqual(live.gap_windows([], timedelta(hours=1), now, depth), [(now - depth, now)])
