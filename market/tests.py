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
        self.clear()
        pair = Pair.objects.get(code="CNY")
        self.perp = Instrument.objects.create(pair=pair, code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")

    def tearDown(self):
        self.clear()

    def clear(self):
        rebuild._dirty.clear()
        live._book.clear()
        live._book_dirty.clear()
        live._socket_bars.clear()

    def test_tick_from_previous_hour_opens_new_hour(self):
        now = datetime(2026, 10, 5, 11, 0, 5, tzinfo=UTC)
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now - timedelta(seconds=15), Decimal("12.10"))
            self.assertFalse(Candle.objects.exists())
            self.assertEqual(live.flush_book(), 1)
        hour = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
        self.assertIn("новый час", text)
        self.assertEqual(Candle.objects.get(instrument=self.perp).ts, hour)
        self.assertEqual(rebuild._dirty, {"CNY": hour})

    def test_ticks_are_written_once_and_merged_with_exchange_bar(self):
        now = datetime(2026, 10, 5, 11, 20, tzinfo=UTC)
        hour = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
        Candle.objects.create(
            instrument=self.perp, ts=hour, open=Decimal("12.00"), high=Decimal("12.05"),
            low=Decimal("11.98"), close=Decimal("12.01"), volume=Decimal("50"),
        )
        with frozen(now):
            for price in ("12.02", "12.20", "11.90", "12.07"):
                live.book_quote(self.perp, now, Decimal(price))
            self.assertEqual(live.flush_book(), 1)
            self.assertEqual(live.flush_book(), 0)
        candle = Candle.objects.get(instrument=self.perp, ts=hour)
        self.assertEqual(
            (candle.open, candle.high, candle.low, candle.close, candle.volume),
            (Decimal("12.00"), Decimal("12.20"), Decimal("11.90"), Decimal("12.07"), Decimal("50")),
        )

    def test_stale_snapshot_is_skipped(self):
        now = datetime(2026, 10, 5, 11, 0, 5, tzinfo=UTC)
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now - timedelta(hours=3), Decimal("12.10"))
            live.flush_book()
        self.assertIn("старая котировка", text)
        self.assertFalse(Candle.objects.exists())
        self.assertEqual(rebuild._dirty, {})

    def test_weekend_is_skipped(self):
        now = datetime(2026, 10, 10, 11, 0, 5, tzinfo=UTC)  # суббота
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now, Decimal("12.10"))
            live.flush_book()
        self.assertIn("биржа закрыта", text)
        self.assertFalse(Candle.objects.exists())


class LiveSymbolsTests(TestCase):
    def setUp(self):
        Pair.objects.filter(code="CNY").update(active=False)
        self.usd = Pair.objects.create(
            code="USD", title="доллар", future_root="Si", perp_code="USDRUBF", perp_symbol="USDRUBF@RTSX",
            spot_code="USDRUB_TOM", spot_symbol="USD000UTSTOM@MISX", foreign_source="DFEDTARU",
        )
        Pair.objects.create(
            code="SICAL", title="Si", base="USD", future_root="Si", perp_code="", perp_symbol="",
            spot_code="", spot_symbol="", foreign_source="CAL",
        )

    def test_front_and_second_move_after_roll(self):
        with frozen(datetime(2026, 12, 17, 12, 0, tzinfo=UTC)):
            before = live.live_symbols()
        with frozen(datetime(2026, 12, 18, 5, 0, tzinfo=UTC)):
            after = live.live_symbols()
        self.assertEqual(sorted(before), sorted(["SiZ6@RTSX", "SiH7@RTSX", "USDRUBF@RTSX", "USD000UTSTOM@MISX"]))
        self.assertEqual(sorted(after), sorted(["SiH7@RTSX", "SiM7@RTSX", "USDRUBF@RTSX", "USD000UTSTOM@MISX"]))
        second = Instrument.objects.get(code="SiM2027")
        self.assertEqual((second.pair, second.finam_symbol), (self.usd, "SiM7@RTSX"))
        self.assertEqual(second.listed_at, Instrument.objects.get(code="SiH2027").roll_at)


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


class BrentCalendarTests(SimpleTestCase):
    def test_expiry_is_last_london_business_day_two_months_before(self):
        from market.brent import expiry
        self.assertEqual(expiry(2027, 1), datetime(2026, 11, 30).date())
        self.assertEqual(expiry(2027, 2), datetime(2026, 12, 31).date())
        # 31.05.2027 — весенний банковский выходной, 30.03.2029 — Страстная пятница.
        self.assertEqual(expiry(2027, 7), datetime(2027, 5, 28).date())
        self.assertEqual(expiry(2029, 5), datetime(2029, 3, 29).date())

    def test_moex_month_is_one_before_nymex(self):
        from market.brent import windows
        rows = windows(datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 11, tzinfo=UTC))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["code"], row["secid"], row["nymex_code"], row["nymex_secid"]),
            ("BRZ2026", "BRZ6", "BZF2027", "BZF27"),
        )
        self.assertEqual(row["listed_at"], datetime(2026, 10, 30, 20, 50, tzinfo=UTC))
        self.assertEqual(row["roll_at"], datetime(2026, 11, 30, 20, 50, tzinfo=UTC))


class BrentLiveSymbolsTests(TestCase):
    def setUp(self):
        Pair.objects.filter(code="CNY").update(active=False)
        self.pair = Pair.objects.create(
            code="BRNYMEX", title="BR MOEX − BZ NYMEX", future_root="BR", perp_code="", perp_symbol="",
            spot_code="", spot_symbol="", foreign_source="NYMEX", quote="USD",
        )

    def test_month_moves_after_bz_expiry(self):
        with frozen(datetime(2026, 11, 30, 12, 0, tzinfo=UTC)):
            before = live.live_symbols()
        with frozen(datetime(2026, 12, 1, 5, 0, tzinfo=UTC)):
            after = live.live_symbols()
        self.assertEqual(before, ["BRZ6@RTSX", "BZF27@XNYM"])
        self.assertEqual(after, ["BRF7@RTSX", "BZG27@XNYM"])
        leg = Instrument.objects.get(code="BZG2027")
        self.assertEqual((leg.pair, leg.kind, leg.finam_symbol), (self.pair, "foreign", "BZG27@XNYM"))
        self.assertEqual(live.find_instrument("BZG27@XNYM"), leg)
