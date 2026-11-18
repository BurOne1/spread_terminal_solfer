"""5-минутки: сессии, запись, выбор актуальных ног, загрузка из Finam, import_tv, онлайн-поток."""

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from market import live, sessions
from market.minutes import bucket, current_instruments, load_history, save_minutes
from market.models import Candle, Instrument, MinuteCandle, Pair

UTC = timezone.utc


class SessionTests(SimpleTestCase):
    def test_forts_weekdays_and_weekend_sessions(self):
        monday, saturday = datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 21, tzinfo=UTC)
        self.assertTrue(sessions.in_session(sessions.FORTS, monday.replace(hour=4)))
        self.assertTrue(sessions.in_session(sessions.FORTS, monday.replace(hour=20, minute=45)))
        self.assertFalse(sessions.in_session(sessions.FORTS, monday.replace(hour=20, minute=50)))
        self.assertFalse(sessions.in_session(sessions.FORTS, saturday.replace(hour=6, minute=55)))
        self.assertTrue(sessions.in_session(sessions.FORTS, saturday.replace(hour=7)))
        self.assertFalse(sessions.in_session(sessions.FORTS, saturday.replace(hour=16)))
        # Часовой бар 20:00 задевает торги до 20:50; бар выходного 15:00 — до 16:00.
        self.assertTrue(sessions.forts_hour(monday.replace(hour=20)))
        self.assertTrue(sessions.forts_hour(saturday.replace(hour=15)))
        self.assertFalse(sessions.forts_hour(saturday.replace(hour=16)))

    def test_spots_cme_and_kinds(self):
        monday, sunday = datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 22, tzinfo=UTC)
        self.assertEqual(sessions.kind_of("GLDRUB_TOM@MISX"), sessions.METAL_SPOT)
        self.assertEqual(sessions.kind_of("USD000UTSTOM@MISX"), sessions.FX_SPOT)
        self.assertEqual(sessions.kind_of("CNHZ26@XCME"), sessions.CME)
        self.assertEqual(sessions.kind_of("SiZ6@RTSX"), sessions.FORTS)
        self.assertEqual(sessions.kind_of("BTCUSDT", "bybit"), sessions.ALWAYS)
        self.assertFalse(sessions.in_session(sessions.METAL_SPOT, monday.replace(hour=5, minute=55)))
        self.assertTrue(sessions.in_session(sessions.METAL_SPOT, monday.replace(hour=15, minute=55)))
        self.assertFalse(sessions.in_session(sessions.METAL_SPOT, sunday.replace(hour=10)))
        self.assertFalse(sessions.in_session(sessions.CME, sunday.replace(hour=21)))
        self.assertTrue(sessions.in_session(sessions.CME, sunday.replace(hour=22, minute=5)))
        self.assertFalse(sessions.in_session(sessions.CME, monday.replace(hour=21, minute=30)))


class FakeClient:
    """Finam: 5-минутки каждые 5 минут, но только после since."""

    def __init__(self, since):
        self.since = since
        self.calls = []

    def bars(self, symbol, start, end, frame):
        self.calls.append((symbol, start, end, frame))
        if end <= self.since:
            return []
        begin = max(start, self.since)
        out, ts = [], bucket(begin)
        while ts < end and len(out) < 50:
            out.append({"ts": ts, "open": Decimal("1"), "high": Decimal("2"), "low": Decimal("0.5"),
                        "close": Decimal("1.5"), "volume": Decimal("3")})
            ts += timedelta(minutes=5)
        return out


class MinutesTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
        self.pair = Pair.objects.get(code="CNY")
        Pair.objects.filter(pk=self.pair.pk).update(perp_code="CNYRUBF", spot_code="CNYRUB_TOM")
        self.pair.refresh_from_db()
        self.perp = Instrument.objects.create(pair=self.pair, code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")
        self.spot = Instrument.objects.create(pair=self.pair, code="CNYRUB_TOM", finam_symbol="CNYRUB_TOM@MISX", kind="spot")
        rolls = {"CRU2026": -60, "CRZ2026": 29, "CRH2027": 120, "CRM2027": 210}
        for code, days in rolls.items():
            Instrument.objects.create(pair=self.pair, code=code, finam_symbol=f"{code[:3]}{code[-1]}@RTSX", kind="future",
                                      roll_at=self.now + timedelta(days=days), listed_at=self.now + timedelta(days=days - 91))

    def test_current_legs_front_second_perp_spot(self):
        codes = [item.code for item, _pair in current_instruments(self.now, "CNY")]
        # Истёкший U2026 и дальний M2027 не грузим; кварталы раньше вечного и спота.
        self.assertEqual(codes, ["CRZ2026", "CRH2027", "CNYRUBF", "CNYRUB_TOM"])

    def test_save_minutes_buckets_and_replaces(self):
        ts = datetime(2026, 11, 18, 10, 7, tzinfo=UTC)
        bar = {"ts": ts, "open": Decimal("1"), "high": Decimal("2"), "low": Decimal("1"), "close": Decimal("1.5")}
        self.assertEqual(save_minutes(self.perp, [bar]), 1)
        save_minutes(self.perp, [{**bar, "close": Decimal("1.7")}])
        row = MinuteCandle.objects.get()
        self.assertEqual((row.ts, row.close), (datetime(2026, 11, 18, 10, 5, tzinfo=UTC), Decimal("1.7")))

    def test_history_goes_back_by_windows_until_empty(self):
        client = FakeClient(since=self.now - timedelta(days=40))
        total = load_history(client, self.perp, self.now - timedelta(days=400), self.now, sleep=lambda _s: None)
        # 28 дн., ещё 28 (до 56 дн. — граница данных), потом два пустых окна и стоп.
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(all(call[3] == "TIME_FRAME_M5" and call[2] - call[1] <= timedelta(days=28) for call in client.calls))
        self.assertEqual(total, MinuteCandle.objects.count())
        self.assertGreater(total, 0)

    def test_dry_run_lists_instruments(self):
        out = StringIO()
        call_command("sync_m5", "--pair", "CNY", "--dry-run", stdout=out)
        self.assertIn("CRZ2026", out.getvalue())
        self.assertIn("CNYRUB_TOM", out.getvalue())


class ImportTvMinutesTests(TestCase):
    def test_five_minute_file_fills_minutes_and_hours(self):
        pair = Pair.objects.get(code="CNY")
        Instrument.objects.create(pair=pair, code="CRZ2026", finam_symbol="CRZ6@RTSX", kind="future",
                                  roll_at=datetime(2026, 12, 17, 20, 50, tzinfo=UTC), listed_at=datetime(2026, 9, 17, 20, 50, tzinfo=UTC))
        start = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        lines = ["time,open,high,low,close,Volume"]
        for index in range(24):  # два часа по 5 минут
            price = 12 + index / 100
            lines.append(f"{int((start + timedelta(minutes=5 * index)).timestamp())},{price},{price + 0.05},{price - 0.05},{price},10")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "RUS_CRZ2026, 5_abc.csv").write_text("\n".join(lines), encoding="utf-8")
            out = StringIO()
            call_command("import_tv", folder, stdout=out)
        self.assertEqual(MinuteCandle.objects.count(), 24)
        self.assertEqual(Candle.objects.count(), 2)
        hour = Candle.objects.order_by("ts").first()
        first = MinuteCandle.objects.order_by("ts").first()
        last_of_hour = MinuteCandle.objects.filter(ts__lt=start + timedelta(hours=1)).order_by("-ts").first()
        self.assertEqual((hour.open, hour.close), (first.open, last_of_hour.close))
        self.assertIn("5 мин: 24", out.getvalue())


class LiveMinutesTests(TestCase):
    def setUp(self):
        self.clear()
        self.perp = Instrument.objects.create(pair=Pair.objects.get(code="CNY"), code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")

    def tearDown(self):
        self.clear()

    def clear(self):
        from strategy import rebuild
        rebuild._dirty.clear()
        rebuild._dirty5.clear()
        for name in ("_book", "_book_dirty", "_socket_bars", "_book5", "_book5_dirty"):
            getattr(live, name).clear()

    def test_quotes_build_five_minute_candle(self):
        from market.tests import frozen
        now = datetime(2026, 11, 21, 11, 7, 5, tzinfo=UTC)  # суббота, сессия выходного дня
        with frozen(now):
            live.save_quote("CNYRUBF@RTSX", now, Decimal("12.10"))
            live.save_quote("CNYRUBF@RTSX", now, Decimal("12.30"))
            live.save_quote("CNYRUBF@RTSX", now, Decimal("12.20"))
            live.flush_book()
        row = MinuteCandle.objects.get()
        self.assertEqual((row.ts, row.open, row.high, row.low, row.close),
                         (datetime(2026, 11, 21, 11, 5, tzinfo=UTC), Decimal("12.10"), Decimal("12.30"), Decimal("12.10"), Decimal("12.20")))

    def test_exchange_bars_replace_quote_candles(self):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
        client = FakeClient(since=now - timedelta(minutes=30))
        with patch("market.live.FinamClient") as maker:
            maker.return_value.__enter__.return_value = client
            count = live.refresh_minutes(["CNYRUBF@RTSX"], now=now)
        self.assertEqual(count, 6)
        self.assertEqual(client.calls[0][1], now - live.MINUTES_GAP)


class HoursFromMinutesTests(TestCase):
    def setUp(self):
        self.perp = Instrument.objects.create(pair=Pair.objects.get(code="CNY"), code="CNYRUBF", finam_symbol="CNYRUBF@RTSX", kind="perp")
        self.hour = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

    def minute(self, minutes, price):
        MinuteCandle.objects.create(instrument=self.perp, ts=self.hour + timedelta(minutes=minutes),
                                    open=price, high=price + 1, low=price - 1, close=price + Decimal("0.5"), volume=2)

    def test_hour_is_built_from_its_minutes(self):
        from market.minutes import hours_from_minutes
        for index, price in enumerate((Decimal("10"), Decimal("12"), Decimal("9"))):
            self.minute(index * 5, price)
        self.assertEqual(hours_from_minutes(self.perp, self.hour + timedelta(minutes=5)), self.hour)
        candle = Candle.objects.get()
        self.assertEqual((candle.open, candle.high, candle.low, candle.close, candle.volume),
                         (Decimal("10"), Decimal("13"), Decimal("8"), Decimal("9.5"), Decimal("6")))

    def test_partial_first_hour_is_left_alone(self):
        from market.minutes import hours_from_minutes
        Candle.objects.create(instrument=self.perp, ts=self.hour, open=1, high=1, low=1, close=1)
        self.minute(30, Decimal("10"))
        self.minute(65, Decimal("11"))  # следующий час, с 11:05 — тоже без первой 5-минутки, но окно с 11:00
        written = hours_from_minutes(self.perp, self.hour + timedelta(minutes=30), fetched_from=self.hour + timedelta(minutes=30))
        self.assertEqual(Candle.objects.get(ts=self.hour).open, Decimal("1"))
        self.assertEqual(written, self.hour + timedelta(hours=1))
