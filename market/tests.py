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
        live._book5.clear()
        live._book5_dirty.clear()

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

    def test_weekend_outside_session_is_skipped(self):
        now = datetime(2026, 10, 10, 18, 0, 5, tzinfo=UTC)  # суббота после сессии выходного дня
        with frozen(now):
            text = live.save_quote("CNYRUBF@RTSX", now, Decimal("12.10"))
            live.flush_book()
        self.assertIn("биржа закрыта", text)
        self.assertFalse(Candle.objects.exists())

    def test_weekend_session_is_written(self):
        now = datetime(2026, 10, 10, 11, 0, 5, tzinfo=UTC)  # суббота, сессия выходного дня 07–16 UTC
        with frozen(now):
            live.save_quote("CNYRUBF@RTSX", now, Decimal("12.10"))
            live.flush_book()
        self.assertEqual(Candle.objects.get().ts, datetime(2026, 10, 10, 11, 0, tzinfo=UTC))


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
    def test_roll_is_last_moex_business_day_of_front_month(self):
        from market.brent import expiry
        self.assertEqual(expiry(2027, 1), datetime(2026, 11, 30).date())
        # 31.12.2026 Мосбиржа не работает.
        self.assertEqual(expiry(2027, 2), datetime(2026, 12, 30).date())
        # Лондонские праздники не в счёт: 31.05.2027 и 30.03.2029 — рабочие дни Мосбиржи.
        self.assertEqual(expiry(2027, 7), datetime(2027, 5, 31).date())
        self.assertEqual(expiry(2029, 5), datetime(2029, 3, 30).date())

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



class ExpiryTests(SimpleTestCase):
    def test_exchange_rules(self):
        from market.expiry import cme_fx_expiry, moex_last_business_day
        self.assertEqual(cme_fx_expiry(2026, 12), datetime(2026, 12, 14).date())
        self.assertEqual(cme_fx_expiry(2027, 3), datetime(2027, 3, 15).date())
        self.assertEqual(moex_last_business_day(2026, 11), datetime(2026, 11, 30).date())
        self.assertEqual(moex_last_business_day(2026, 12), datetime(2026, 12, 30).date())
        self.assertEqual(moex_last_business_day(2027, 1), datetime(2027, 1, 29).date())

    def test_finam_expiry_fields(self):
        from market.expiry import finam_expiry
        from market.finam import FinamError

        class Client:
            def __init__(self, body):
                self.body = body

            def asset(self, symbol):
                if self.body is None:
                    raise FinamError("нет", status=404)
                return self.body
        self.assertEqual(finam_expiry(Client({"futureDetails": {"expirationDate": "2026-11-25T00:00:00Z"}}), "X"), datetime(2026, 11, 25).date())
        # Полночь по Москве приходит как 21:00 UTC предыдущего дня.
        self.assertEqual(finam_expiry(Client({"futureDetails": {"expirationDate": "2026-12-13T21:00:00Z"}}), "X"), datetime(2026, 12, 14).date())
        self.assertEqual(finam_expiry(Client({"expiration_date": {"year": 2026, "month": 12, "day": 14}}), "X"), datetime(2026, 12, 14).date())
        self.assertIsNone(finam_expiry(Client(None), "X"))
        self.assertIsNone(finam_expiry(None, "X"))

    def test_cross_and_ng_windows(self):
        from market import cross, ngcal
        rows = cross.windows(cross.CROSS["SI6E"], datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC))
        self.assertEqual(
            (rows[0]["code"], rows[0]["a"], rows[0]["symbol"], rows[0]["c"]),
            ("6EZ2026", "SiZ6@RTSX", "6EZ26@XCME", "EuZ6@RTSX"),
        )
        self.assertEqual(rows[0]["roll_at"], datetime(2026, 12, 14, 14, 0, tzinfo=UTC))
        moved = cross.windows(cross.CROSS["SI6E"], datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC),
                              {"6EZ26@XCME": datetime(2026, 12, 11).date()})
        self.assertEqual(moved[0]["roll_at"], datetime(2026, 12, 11, 14, 0, tzinfo=UTC))
        ng = ngcal.windows(datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 11, tzinfo=UTC))
        self.assertEqual((ng[0]["symbol"], ng[0]["front"]), ("NGX6NGZ6@RTSX", "NGX6@RTSX"))
        self.assertEqual(ng[0]["roll_at"], datetime(2026, 11, 30, 20, 50, tzinfo=UTC))
        december = ngcal.windows(datetime(2026, 12, 10, tzinfo=UTC), datetime(2026, 12, 11, tzinfo=UTC))
        self.assertEqual(december[0]["symbol"], "NGZ6NGF7@RTSX")


class ExtraLiveSymbolsTests(TestCase):
    def test_cross_and_ng_move_after_expiry(self):
        Pair.objects.filter(code="CNY").update(active=False)
        Pair.objects.create(code="CRCNH", title="x", future_root="CR", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CMEFX", lot=1)
        Pair.objects.create(code="NGCAL", title="x", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="LISTED", lot=100)
        with frozen(datetime(2026, 12, 14, 12, 0, tzinfo=UTC)):
            before = live.live_symbols()
        with frozen(datetime(2026, 12, 14, 15, 0, tzinfo=UTC)):
            after = live.live_symbols()
        # NGCAL — одна котировка, без ноги фронта.
        self.assertEqual(before, ["CNHZ26@XCME", "CRZ6@RTSX", "SiZ6@RTSX", "NGZ6NGF7@RTSX"])
        self.assertEqual(after[:3], ["CNHH27@XCME", "CRH7@RTSX", "SiH7@RTSX"])


class OwnPairTypeTests(TestCase):
    def test_own_pair_with_similar_type_is_not_cross(self):
        from market.management.commands.sync_bars import _targets
        own = Pair.objects.create(code="CRUSI", title="своя", future_root="CR", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CROSS")
        made = Pair.objects.create(code="CRCNH", title="x", future_root="CR", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="CMEFX")
        self.assertEqual((own.cross, own.zero_fair, made.cross, made.zero_fair), (False, False, True, True))
        start = datetime(2026, 11, 1, tzinfo=UTC)
        self.assertEqual(_targets(own, start, start + timedelta(days=10)), [])


class SyncBarsMinutesTests(TestCase):
    """sync_bars: из Finam только 5-минутки, не глубже --days, часы — из них."""

    def test_loads_m5_within_days_and_builds_hours(self):
        from decimal import Decimal
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings

        from market.models import Candle, MinuteCandle

        pair = Pair.objects.create(code="XX", title="x", future_root="", perp_code="XXF", perp_symbol="XXF@RTSX",
                                   spot_code="", spot_symbol="", foreign_source="NONE")
        perp = Instrument.objects.create(pair=pair, code="XXF", finam_symbol="XXF@RTSX", kind="perp")
        end = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)  # среда
        calls = []

        class Fake:
            def __init__(self, *args):
                pass

            def bars(self, symbol, start, finish, frame):
                calls.append((symbol, start, finish, frame))
                rows = []
                ts = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
                for step in range(12):
                    if start <= ts < finish:
                        price = Decimal(100 + step)
                        rows.append({"ts": ts, "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1})
                    ts += timedelta(minutes=5)
                return rows

            def close(self):
                pass

        out = StringIO()
        with override_settings(FINAM_API_SECRET="test"), \
                mock.patch("market.management.commands.sync_bars.FinamClient", Fake):
            call_command("sync_bars", "--pair", "XX", "--days", "40", "--from", "2026-01-01", "--to", end.isoformat(), stdout=out)
            with self.assertRaises(CommandError):
                call_command("sync_bars", "--days", "500", stdout=StringIO())
        self.assertTrue(calls)
        self.assertEqual({frame for *_rest, frame in calls}, {"TIME_FRAME_M5"})
        # --from старше границы обрезан до end − 40 дней.
        self.assertEqual(min(start for _s, start, _f, _fr in calls), end - timedelta(days=40))
        self.assertIn("раньше границы в 40 дн.", out.getvalue())
        self.assertEqual(MinuteCandle.objects.filter(instrument=perp).count(), 12)
        hour = Candle.objects.get(instrument=perp)
        self.assertEqual(hour.ts, datetime(2026, 11, 18, 10, 0, tzinfo=UTC))
        self.assertEqual((hour.open, hour.high, hour.low, hour.close, hour.volume),
                         (Decimal(100), Decimal(112), Decimal(99), Decimal(111), 12))


class AddUsdTests(TestCase):
    def test_creates_pair_and_contracts(self):
        from django.core.management import call_command
        from io import StringIO
        call_command("add_usd", stdout=StringIO())
        pair = Pair.objects.get(code="USD")
        self.assertEqual((pair.future_root, pair.future_scale, pair.spot_symbol), ("Si", 1000, "USD000UTSTOM@MISX"))
        self.assertTrue(Instrument.objects.filter(pair=pair, kind="future", finam_symbol__startswith="Si").exists())
        self.assertEqual(Instrument.objects.get(code="USDRUB_TOM").finam_symbol, "USD000UTSTOM@MISX")


class GasNymexWindowTests(TestCase):
    def test_windows_and_live_symbols(self):
        from market import ngnymex
        from analytics.data import contract_month
        rows = ngnymex.windows(datetime(2026, 12, 10, tzinfo=UTC), datetime(2026, 12, 11, tzinfo=UTC))
        self.assertEqual((rows[0]["code"], rows[0]["moex"], rows[0]["symbol"]), ("NGZ2026-NYM", "NGZ6@RTSX", "NGF27@XNYM"))
        self.assertEqual(contract_month("NGZ2026-NYM", None), ("Z", 2026))
        Pair.objects.filter(code="CNY").update(active=False)
        Pair.objects.create(code="NGNYMEX", title="x", future_root="NG", perp_code="", perp_symbol="", spot_code="", spot_symbol="", foreign_source="NGNYM", lot=100)
        with frozen(datetime(2026, 11, 30, 12, 0, tzinfo=UTC)):
            before = live.live_symbols()
        with frozen(datetime(2026, 12, 1, 5, 0, tzinfo=UTC)):
            after = live.live_symbols()
        self.assertEqual(before, ["NGZ26@XNYM", "NGX6@RTSX"])
        self.assertEqual(after, ["NGF27@XNYM", "NGZ6@RTSX"])


class SingleDefaultLevelsTests(TestCase):
    def test_migration_keeps_first_entry_and_first_take(self):
        import importlib
        from django.apps import apps as django_apps
        from market.models import Pair
        migration = importlib.import_module("market.migrations.0008_single_default_levels")
        pair = Pair.objects.create(code="LADDER", title="лесенка", future_root="LL", perp_code="", perp_symbol="",
                                   spot_code="", spot_symbol="",
                                   long_levels={"signed": True, "values": [-0.01, -0.02, -0.03]},
                                   short_levels={"signed": True, "values": [0.01, 0.02]},
                                   long_tp={"signed": True, "values": [0.0025, 0.0]},
                                   short_tp={"signed": True, "values": [-0.0025]})
        migration.forwards(django_apps, None)
        pair.refresh_from_db()
        self.assertEqual([pair.long_levels["values"], pair.short_levels["values"], pair.long_tp["values"], pair.short_tp["values"]],
                         [[-0.01], [0.01], [0.0025], [-0.0025]])
        from strategy.engine import pair_levels
        self.assertEqual(pair_levels(None), ([-0.01], [0.01], [0.0025], [-0.0025]))
