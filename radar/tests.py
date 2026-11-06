"""Радар: разбор Finam, стакан, детекторы, запись пачкой, страница без данных."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from market.models import Candle, Instrument, Pair
from radar import algopack
from radar.detect import Book, SymbolWatch
from radar.feed import Radar
from radar.models import Anomaly, BookState, MinuteStat, SourceStatus, Trade, WatchItem
from radar.stream import books_of, payload_of, trades_of, watch_items

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


def trade(seconds, price, size, side, oi=None, trade_id=""):
    return {"trade_id": trade_id, "ts": T0 + timedelta(seconds=seconds), "price": price, "size": size, "side": side, "oi": oi}


def ladder(asks=None, bids=None):
    """Стакан по 10 уровней на сторону объёмом 10, кроме заданных."""
    rows = []
    for index in range(10):
        rows.append(("ask", 101.0 + index, (asks or {}).get(101.0 + index, 10.0), "ACTION_ADD"))
        rows.append(("bid", 100.0 - index, (bids or {}).get(100.0 - index, 10.0), "ACTION_ADD"))
    return rows


def warm(watch, count=100, size=1.0):
    """История мелких сделок, чтобы пороги устоялись."""
    for index in range(count):
        watch.on_trade(trade(index * 2, 100.0, size, 1 if index % 2 else -1))


class ParseTests(SimpleTestCase):
    def test_socket_order_book_with_string_payload(self):
        message = {"type": "DATA", "subscription_type": "ORDER_BOOK", "payload": json.dumps({"order_book": [{
            "symbol": "SiZ6@RTSX",
            "rows": [
                {"price": {"value": "95000"}, "buy_size": {"value": "10"}, "action": "ACTION_ADD"},
                {"price": "95010", "sell_size": "5", "action": "ACTION_UPDATE"},
            ],
        }]})}
        self.assertEqual(books_of(payload_of(message)), [("SiZ6@RTSX", [
            ("bid", 95000.0, 10.0, "ACTION_ADD"), ("ask", 95010.0, 5.0, "ACTION_UPDATE"),
        ])])

    def test_real_socket_messages_in_camel_case(self):
        # Сообщения из вывода run_radar --dump на ВМ, 03.11.2026.
        book = {"type": "DATA", "subscription_type": "ORDER_BOOK", "subscription_key": "BRZ6@RTSX", "payload": json.dumps({
            "orderBook": [{"symbol": "BRZ6@RTSX", "rows": [
                {"price": {"value": "106.38"}, "sellSize": {"value": "168.0"}, "action": "ACTION_UPDATE",
                 "timestamp": "2026-11-03T06:09:47.090618531Z"},
                {"price": {"value": "105.89"}, "buySize": {"value": "101.0"}, "action": "ACTION_UPDATE",
                 "timestamp": "2026-11-03T06:09:28.304923986Z"},
            ]}]})}
        self.assertEqual(books_of(payload_of(book)), [("BRZ6@RTSX", [
            ("ask", 106.38, 168.0, "ACTION_UPDATE"), ("bid", 105.89, 101.0, "ACTION_UPDATE"),
        ])])
        trades = {"type": "DATA", "subscription_type": "INSTRUMENT_TRADES", "payload": json.dumps({
            "symbol": "CNYRUBF@RTSX", "trades": [{
                "tradeId": "2024119555760068128", "timestamp": "2026-11-03T06:09:42.691882393Z",
                "price": {"value": "12.232"}, "size": {"value": "1.0"}, "side": "SIDE_BUY",
                "openInterest": {"value": "2.0666016E7"}, "isDataSnapshot": True,
            }]})}
        symbol, items = trades_of(payload_of(trades))
        self.assertEqual(symbol, "CNYRUBF@RTSX")
        self.assertEqual(
            (items[0]["trade_id"], items[0]["side"], items[0]["oi"], items[0]["snapshot"]),
            ("2024119555760068128", 1, 20666016.0, True),
        )

    def test_unknown_symbol_error(self):
        from market.finam import FinamError
        from radar.stream import missing
        self.assertTrue(missing(FinamError("Finam 404 CRZ6@MISX: Security id doesn't exist", status=404)))
        self.assertFalse(missing(FinamError("Finam 504 USDCNY_TOM@MISX: Deadline is too short", status=504)))

    def test_rest_order_book(self):
        payload = {"symbol": "SiZ6@RTSX", "orderbook": {"rows": [{"price": {"value": "1"}, "sell_size": {"value": "2"}}]}}
        self.assertEqual(books_of(payload), [("SiZ6@RTSX", [("ask", 1.0, 2.0, "ACTION_UNSPECIFIED")])])

    def test_trades(self):
        symbol, trades = trades_of({"symbol": "SiZ6@RTSX", "trades": [
            {"trade_id": "7", "timestamp": "2026-11-03T08:00:01.250Z", "price": {"value": "95000"},
             "size": {"value": "3"}, "side": "SIDE_SELL", "open_interest": {"value": "1200"}, "is_data_snapshot": True},
            {"trade_id": "8", "timestamp": "2026-11-03T08:00:02Z", "price": "95001", "size": "1", "side": "SIDE_BUY"},
        ]})
        self.assertEqual(symbol, "SiZ6@RTSX")
        self.assertEqual([(item["side"], item["oi"], item["snapshot"]) for item in trades], [(-1, 1200.0, True), (1, None, False)])
        self.assertEqual(trades[0]["ts"], T0 + timedelta(seconds=1, milliseconds=250))


class BookTests(SimpleTestCase):
    def test_full_snapshot_replaces_and_updates_apply(self):
        book = Book()
        book.apply(ladder())
        book.apply([("ask", 101.0, 0, "ACTION_REMOVE"), ("bid", 100.0, 25.0, "ACTION_UPDATE")])
        asks, bids = book.top(2)
        self.assertEqual(asks, [(102.0, 10.0), (103.0, 10.0)])
        self.assertEqual(bids[0], (100.0, 25.0))
        book.apply(ladder())
        self.assertEqual(book.top(1)[0], [(101.0, 10.0)])

    def test_crossed_book_drops_stale_side(self):
        book = Book()
        book.apply(ladder())
        book.apply([("bid", 102.5, 4.0, "ACTION_ADD")])
        asks, bids = book.top(1)
        self.assertEqual((asks[0][0], bids[0][0]), (103.0, 102.5))


class DetectTests(SimpleTestCase):
    def setUp(self):
        self.watch = SymbolWatch("SiZ6@RTSX", min_sizes=50)

    def test_large_print_after_history(self):
        warm(self.watch)
        self.assertEqual(self.watch.on_trade(trade(300, 100.0, 25.0, -1)), [])
        self.watch.on_trade(trade(300.002, 100.0, 15.0, -1))
        found = self.watch.tick(T0 + timedelta(seconds=301))
        self.assertEqual([(item["kind"], item["side"], item["size"]) for item in found], [("large", -1, 40.0)])
        self.assertIn("продавец: 40 лотов одной заявкой, 2 сделками", found[0]["text"])

    def test_no_large_print_before_history(self):
        warm(self.watch, count=10)
        self.watch.on_trade(trade(300, 100.0, 40.0, -1))
        self.assertEqual(self.watch.tick(T0 + timedelta(seconds=301)), [])

    def test_history_replay_fills_stats_without_alerts(self):
        found = []
        for index in range(400):
            found += self.watch.on_trade(trade(index, 100.0, 1.0 if index % 50 else 90.0, 1 if index % 2 else -1), detect=False)
        found += self.watch.tick(T0 + timedelta(minutes=10))
        self.assertEqual(found, [])
        self.assertEqual(len(self.watch.sizes.sizes), 400)
        self.assertNotIn("quiet", self.watch.closed[0])

    def test_sweep_through_three_levels(self):
        warm(self.watch)
        start = 300
        for price in (100.0, 100.5, 101.0):
            self.watch.on_trade(trade(start, price, 2.0, 1))
        found = self.watch.tick(T0 + timedelta(seconds=start + 1))
        # Одна заявка — одно событие: проход, без отдельных крупных сделок.
        self.assertEqual([item["kind"] for item in found], ["sweep"])
        self.assertIn("прошёл 3 уровня одной заявкой: 3 сделки, 6 лотов", found[0]["text"])

    def warm_book(self):
        """Сто секунд обычного стакана: история уровней для порога стены."""
        for index in range(110):
            self.watch.on_book(ladder(), T0 - timedelta(seconds=200 - index))

    def test_no_walls_before_book_history(self):
        warm(self.watch)
        book = ladder(asks={103.0: 200.0})
        self.watch.on_book(book, T0)
        self.assertEqual(self.watch.on_book(book, T0 + timedelta(seconds=40)), [])

    def test_wall_eaten_and_pulled(self):
        warm(self.watch)
        self.warm_book()
        book = ladder(asks={103.0: 200.0}, bids={97.0: 150.0})
        self.assertEqual(self.watch.on_book(book, T0), [])
        self.assertEqual(self.watch.on_book(book, T0 + timedelta(seconds=10)), [])
        appeared = self.watch.on_book(book, T0 + timedelta(seconds=31))
        self.assertEqual(sorted(item["kind"] for item in appeared), ["wall", "wall"])
        self.assertEqual(self.watch.levels()[0][2], [103.0, 200.0, True])
        for index in range(3):
            self.watch.on_trade(trade(400 + index, 103.0, 60.0, 1))
        gone = self.watch.on_book(
            [("ask", 103.0, 0, "ACTION_REMOVE"), ("bid", 97.0, 0, "ACTION_REMOVE")], T0 + timedelta(seconds=40),
        )
        self.assertEqual({item["price"]: item["kind"] for item in gone}, {103.0: "eaten", 97.0: "pulled"})

    def test_far_wall_removed_quietly(self):
        warm(self.watch)
        self.warm_book()
        book = ladder(bids={92.0: 150.0})
        self.watch.on_book(book, T0)
        self.assertEqual([item["kind"] for item in self.watch.on_book(book, T0 + timedelta(seconds=31))], ["wall"])
        self.assertEqual(self.watch.on_book([("bid", 92.0, 0, "ACTION_REMOVE")], T0 + timedelta(seconds=40)), [])

    def test_flickering_wall_is_not_announced(self):
        warm(self.watch)
        self.warm_book()
        self.watch.on_book(ladder(asks={103.0: 200.0}), T0)
        gone = self.watch.on_book([("ask", 103.0, 0, "ACTION_REMOVE")], T0 + timedelta(seconds=5))
        self.assertEqual(gone, [])
        self.assertEqual(self.watch.walls, {})

    def test_volume_spike_against_recent_minutes(self):
        for minute in range(40):
            self.watch.on_trade(trade(minute * 60, 100.0, 10.0 + minute % 3, 1))
        found = self.watch.on_trade(trade(40 * 60, 100.0, 300.0, 1))
        found += self.watch.tick(T0 + timedelta(minutes=42))
        self.assertIn("volume", [item["kind"] for item in found])

    def test_open_interest_jump(self):
        oi = 1000.0
        for minute in range(40):
            oi += 1 + minute % 2
            self.watch.on_trade(trade(minute * 60, 100.0, 1.0, 1, oi=oi))
        found = self.watch.on_trade(trade(40 * 60, 100.0, 1.0, 1, oi=oi + 500))
        found += self.watch.on_trade(trade(41 * 60, 99.0, 1.0, -1, oi=oi + 500))
        found += self.watch.tick(T0 + timedelta(minutes=43))
        jumps = [item for item in found if item["kind"] == "oi"]
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]["side"], 1)


def item(symbol, group="legs"):
    return {"symbol": symbol, "title": symbol.split("@")[0], "group": group, "large_pct": 99.5, "wall_ratio": 5.0}


class RadarFlushTests(TestCase):
    def test_flush_writes_trades_minutes_book_and_status(self):
        radar = Radar()
        radar.watch([item("SiZ6@RTSX")])
        radar.on_trades("SiZ6@RTSX", [trade(1, 100.0, 2.0, 1, oi=50, trade_id="1"), trade(2, 101.0, 3.0, -1, trade_id="2")])
        radar.on_trades("SiZ6@RTSX", [trade(1, 100.0, 2.0, 1, oi=50, trade_id="1")])
        radar.on_book("SiZ6@RTSX", ladder(), T0)
        radar.source("finam", "ok", "поток идёт")
        radar.flush(T0 + timedelta(seconds=5))
        self.assertEqual(Trade.objects.count(), 2)
        minute = MinuteStat.objects.get()
        self.assertEqual((float(minute.buy), float(minute.sell), float(minute.close), float(minute.oi)), (2.0, 3.0, 101.0, 50.0))
        book = BookState.objects.get()
        self.assertEqual(book.asks[0][:2], [101.0, 10.0])
        self.assertEqual(book.summary["last"], 101.0)
        self.assertEqual(SourceStatus.objects.get(name="radar").state, "ok")
        self.assertEqual(SourceStatus.objects.get(name="finam").message, "поток идёт")
        radar.watch([item("RIZ6@RTSX", "extra")])
        radar.flush(T0 + timedelta(seconds=6))
        self.assertEqual(list(BookState.objects.values_list("symbol", flat=True)), ["RIZ6@RTSX"])

    def test_cooldown_keeps_one_volume_alert_per_minute(self):
        radar = Radar()
        radar.watch([item("SiZ6@RTSX")])
        radar._found([{"symbol": "SiZ6@RTSX", "ts": T0, "kind": "volume", "side": 1, "price": 1.0, "size": 1.0, "score": 9, "text": ""}] * 2)
        radar.flush(T0)
        self.assertEqual(Anomaly.objects.count(), 1)


class WatchItemsTests(TestCase):
    def test_legs_and_admin_items(self):
        WatchItem.objects.create(code="SiZ6@RTSX", kind="symbol", title="Si", large_pct=99.9)
        with patch("radar.stream.live_symbols", return_value=["SiZ6@RTSX", "CNYRUBF@RTSX"]):
            items = {row["symbol"]: row for row in watch_items(T0)}
        self.assertEqual(set(items), {"SiZ6@RTSX", "CNYRUBF@RTSX", "RIZ6@RTSX", "MXZ6@RTSX", "IMOEXF@RTSX"})
        self.assertEqual((items["SiZ6@RTSX"]["group"], items["SiZ6@RTSX"]["large_pct"]), ("legs", 99.9))
        self.assertEqual((items["RIZ6@RTSX"]["group"], items["RIZ6@RTSX"]["title"]), ("extra", "RTS RIZ2026"))

    def test_quarter_moves_after_roll(self):
        with patch("radar.stream.live_symbols", return_value=[]):
            items = {row["symbol"] for row in watch_items(datetime(2026, 12, 18, 9, 0, tzinfo=UTC))}
        self.assertIn("RIH7@RTSX", items)


class AlgopackTests(SimpleTestCase):
    @override_settings(ALGOPACK_TOKEN="")
    def test_no_token(self):
        self.assertEqual(algopack.check(), ("no_key", "нет ALGOPACK_TOKEN в .env"))


class RadarPageTests(TestCase):
    def test_page_renders_without_any_data(self):
        response = self.client.get("/radar/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="chips"')
        body = self.client.get("/api/radar/state/").json()
        self.assertFalse(body["alive"])
        self.assertEqual([item["state"] for item in body["statuses"]], ["off", "off", "off"])
        self.assertIn("run_radar", body["statuses"][2]["message"])
        self.assertEqual((body["symbols"], body["book"], body["anomalies"]), ([], None, []))

    def test_state_and_chart_from_flushed_radar(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        radar = Radar()
        radar.watch([item("SiZ6@RTSX")])
        radar.source("algopack", "no_key", "нет ALGOPACK_TOKEN в .env")
        start = now - timedelta(minutes=3)
        radar.on_trades("SiZ6@RTSX", [
            {"trade_id": "1", "ts": start, "price": 100.0, "size": 5.0, "side": 1, "oi": 10.0},
            {"trade_id": "2", "ts": start + timedelta(minutes=1), "price": 99.0, "size": 2.0, "side": -1, "oi": 12.0},
        ])
        radar.on_book("SiZ6@RTSX", ladder(), now)
        radar.flush(now)
        body = self.client.get("/api/radar/state/?min=3").json()
        self.assertTrue(body["alive"])
        self.assertEqual({item["name"]: item["state"] for item in body["statuses"]}, {"finam": "off", "algopack": "no_key", "radar": "ok"})
        self.assertEqual(body["symbol"], "SiZ6@RTSX")
        self.assertEqual([row["s"] for row in body["tape"]], [5.0])
        chart = self.client.get("/api/radar/chart/?symbol=SiZ6@RTSX").json()
        self.assertEqual([point["v"] for point in chart["delta"]], [5.0, 3.0])
        self.assertEqual([point["v"] for point in chart["oi"]], [10.0, 12.0])

    def test_instruments_outside_radar(self):
        pair = Pair.objects.get(code="CNY")
        future = Instrument.objects.create(pair=pair, code="CRZ2026", finam_symbol="CRZ6@RTSX", kind="future")
        Candle.objects.create(instrument=future, ts=datetime.now(timezone.utc) - timedelta(hours=1), open=1, high=1, low=1, close=11.5)
        rows = self.client.get("/api/radar/instruments/").json()["instruments"]
        self.assertEqual([(row["code"], row["close"]) for row in rows], [("CRZ2026", 11.5)])
