"""Детекторы радара: чистая логика без базы.

На вход сделки и строки стакана одного символа, на выход словари аномалий.
Пороги — по истории самого инструмента: процентиль размеров его заявок,
медиана уровней его стакана, объём той же минуты суток в прошлые дни.
Мосбиржа анонимна: видны следы участника, а не он сам.
"""

import statistics
from collections import deque
from datetime import timedelta

KEEP_SIZES = 5000
# Пока сделок меньше, порог крупной сделки не устоялся и не проверяется.
MIN_SIZES = 300
SWEEP_GAP = timedelta(milliseconds=100)
SWEEP_LEVELS = 3
# Проход по 3+ уровням — только для заявок крупнее этого процентиля: мелкие проходы
# в тонком стакане (Si с шагом 1) обычны.
SWEEP_PCT = 99.0
BOOK_DEPTH = 20
# От пика стены осталось меньше этой доли — стены больше нет.
WALL_GONE = 0.2
# Исполнилось хотя бы столько от ушедшего объёма — стену съели, иначе сняли.
WALL_EATEN = 0.5
# Стена объявляется, только если простояла столько: перестановки маркетмейкеров не в счёт.
WALL_CONFIRM = timedelta(seconds=30)
# Стена крупнее этого процентиля уровней стакана инструмента за последние часы.
WALL_PCT = 99.5
# Уровни стакана снимаются в историю не чаще раза в секунду: верхние WALL_SAMPLE на сторону.
WALL_SAMPLE = 10
WALL_SAMPLE_EVERY = timedelta(seconds=1)
WALL_MIN_SAMPLES = 2000
# Снятие считается, только если стена стояла не дальше стольких уровней от лучшей цены.
WALL_NEAR = 3
MAX_WALLS = 200
VOLUME_Z = 5.0
VOLUME_RATIO = 3.0
OI_Z = 6.0
# Минут истории, после которых объём и ОИ сравниваются с нормой.
MIN_HISTORY = 30
SEASON_WINDOW = 10
SEASON_MIN = 20


def robust_z(value, sample):
    """(value − медиана) / (1,4826 · MAD): хвосты объёмов не раздувают норму."""
    if len(sample) < 3:
        return None
    middle = statistics.median(sample)
    scale = 1.4826 * statistics.median(abs(item - middle) for item in sample)
    if scale <= 0:
        scale = 1.2533 * sum(abs(item - middle) for item in sample) / len(sample)
    if scale <= 0:
        return None
    return (value - middle) / scale


def sign(value):
    return (value > 0) - (value < 0)


def word(count, one, few, many):
    """Форма слова для числа: 1 уровень, 3 уровня, 5 уровней."""
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def plural(count, one, few, many):
    return f"{count} {word(count, one, few, many)}"


def lots(value):
    """Дробные лоты (спот в граммах) — «лота», целые — по числу."""
    if not float(value).is_integer():
        return f"{fmt(value)} лота"
    return f"{fmt(value)} {word(value, 'лот', 'лота', 'лотов')}"


def fmt(value):
    return f"{value:,.0f}".replace(",", " ") if abs(value) >= 100 else f"{value:g}"


class SizeStats:
    """Скользящее распределение размеров заявок (серий сделок). Сортировка — раз в 100."""

    def __init__(self, keep=KEEP_SIZES, min_samples=MIN_SIZES, resort=100):
        self.sizes = deque(maxlen=keep)
        self.min_samples = min_samples
        self.resort = resort
        self._sorted = None
        self._stale = 0

    def add(self, size):
        self.sizes.append(size)
        self._stale += 1

    def percentile(self, pct):
        if len(self.sizes) < self.min_samples:
            return None
        if self._sorted is None or self._stale >= self.resort:
            self._sorted = sorted(self.sizes)
            self._stale = 0
        return self._sorted[min(len(self._sorted) - 1, int(pct / 100.0 * len(self._sorted)))]


class Book:
    """Стакан символа: цена → объём. Строки: (сторона bid/ask, цена, объём, действие)."""

    def __init__(self):
        self.bids = {}
        self.asks = {}

    def replace(self, rows):
        self.bids, self.asks = {}, {}
        self.apply(rows, snapshot=False)

    def apply(self, rows, snapshot=None):
        """snapshot=None — угадать: только добавления и обе стороны — это весь стакан."""
        if snapshot is None:
            actions = {action for _side, _price, _size, action in rows}
            sides = [side for side, _price, _size, _action in rows]
            snapshot = (
                not actions & {"ACTION_REMOVE", "ACTION_UPDATE"}
                and sides.count("bid") >= 5 and sides.count("ask") >= 5
            )
        if snapshot:
            self.bids, self.asks = {}, {}
        touched = set()
        for side, price, size, action in rows:
            levels = self.bids if side == "bid" else self.asks
            touched.add(side)
            if action == "ACTION_REMOVE" or size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        self._uncross(touched)

    def _uncross(self, touched):
        """Пересечение после пропущенного удаления: старой считается нетронутая сторона."""
        if not self.bids or not self.asks or max(self.bids) < min(self.asks):
            return
        if touched == {"bid"}:
            best = max(self.bids)
            self.asks = {price: size for price, size in self.asks.items() if price > best}
        elif touched == {"ask"}:
            best = min(self.asks)
            self.bids = {price: size for price, size in self.bids.items() if price < best}

    def top(self, depth=BOOK_DEPTH):
        asks = sorted(self.asks.items())[:depth]
        bids = sorted(self.bids.items(), reverse=True)[:depth]
        return asks, bids

    def size_at(self, side, price):
        return (self.bids if side == "bid" else self.asks).get(price, 0.0)


class SymbolWatch:
    """Всё, что радар знает об одном символе, и его детекторы."""

    def __init__(self, symbol, large_pct=99.5, wall_ratio=5.0, min_sizes=MIN_SIZES):
        self.symbol = symbol
        self.large_pct = large_pct
        self.wall_ratio = wall_ratio
        self.book = Book()
        self.sizes = SizeStats(min_samples=min_sizes)
        # Размеры уровней стакана: от них порог стены.
        self.book_sizes = SizeStats(keep=20000, min_samples=WALL_MIN_SAMPLES, resort=500)
        self.sampled = None
        self.burst = None
        self.walls = {}
        self.bar = None
        self.closed = []
        self.recent = deque(maxlen=240)
        self.oi_moves = deque(maxlen=240)
        self.seasonal = {}
        self.last_oi = None
        self.last_price = None
        self.day_open = None
        self.day = None

    # --- лента ---

    def on_trade(self, trade, detect=True):
        """detect=False — история при подключении: наполняет статистику, аномалий не даёт."""
        found = []
        ts, price, size, side = trade["ts"], trade["price"], trade["size"], trade["side"]
        minute = ts.replace(second=0, microsecond=0)
        if self.bar is not None and minute > self.bar["ts"]:
            found += self.close_minute()
        if self.bar is None:
            self.bar = {
                "ts": minute, "open": price, "high": price, "low": price, "close": price,
                "volume": 0.0, "buy": 0.0, "sell": 0.0, "trades": 0, "large": 0, "oi": None, "quiet": False,
            }
        bar = self.bar
        if not detect:
            bar["quiet"] = True
        if minute >= bar["ts"]:
            bar["high"], bar["low"], bar["close"] = max(bar["high"], price), min(bar["low"], price), price
        bar["volume"] += size
        bar["trades"] += 1
        if side > 0:
            bar["buy"] += size
        elif side < 0:
            bar["sell"] += size
        if trade.get("oi") is not None:
            bar["oi"] = trade["oi"]
        day = _moscow_day(ts)
        if day != self.day:
            self.day, self.day_open = day, price
        found += self._burst(trade, detect)
        for key, wall in self.walls.items():
            if key[1] == price:
                wall["traded"] += size
        self.last_price = price
        return found

    def _burst(self, trade, detect=True):
        """Сделки одной стороны подряд — одна агрессивная заявка: биржа исполняет
        её по уровням стакана несколькими сделками почти в одно время."""
        found = []
        burst = self.burst
        if burst is not None and (
            trade["side"] != burst["side"] or trade["ts"] - burst["last"] > SWEEP_GAP or trade["side"] == 0
        ):
            found += self.finish_burst()
        if self.burst is None:
            self.burst = {
                "side": trade["side"], "start": trade["ts"], "last": trade["ts"], "prices": set(),
                "size": 0.0, "count": 0, "first": trade["price"], "price": trade["price"], "quiet": False,
            }
        burst = self.burst
        if not detect:
            burst["quiet"] = True
        burst["last"] = trade["ts"]
        burst["prices"].add(trade["price"])
        burst["size"] += trade["size"]
        burst["count"] += 1
        burst["price"] = trade["price"]
        if trade["side"] == 0:
            found += self.finish_burst()
        return found

    def finish_burst(self):
        """Заявка закончилась: прошла 3+ уровня — проход по стакану, иначе крупная, если велика."""
        burst, self.burst = self.burst, None
        if burst is None:
            return []
        levels, size = len(burst["prices"]), burst["size"]
        sweep = self.sizes.percentile(SWEEP_PCT)
        large = self.sizes.percentile(self.large_pct)
        middle = self.sizes.percentile(50.0) or 0
        self.sizes.add(size)
        if burst["quiet"]:
            return []
        who = {1: "покупатель", -1: "продавец"}.get(burst["side"], "участник")
        found = []
        if sweep is not None and levels >= SWEEP_LEVELS and size >= sweep:
            took = int((burst["last"] - burst["start"]).total_seconds() * 1000)
            how = "одной заявкой" if took == 0 else f"за {took} мс"
            found.append(self._anomaly(
                burst["last"], "sweep", burst["side"], burst["price"], size, size / sweep,
                f"{who} прошёл {plural(levels, 'уровень', 'уровня', 'уровней')} {how}: "
                f"{plural(burst['count'], 'сделка', 'сделки', 'сделок')}, {lots(size)}, {burst['first']:g} → {burst['price']:g}",
            ))
        elif large is not None and size >= large and size >= 3 * middle:
            parts = f", {plural(burst['count'], 'сделкой', 'сделками', 'сделками')}" if burst["count"] > 1 else ""
            found.append(self._anomaly(
                burst["last"], "large", burst["side"], burst["price"], size, size / large,
                f"{who}: {lots(size)} одной заявкой{parts}, порог {fmt(large)} ({self.large_pct:g}-й процентиль)",
            ))
        if found and self.bar is not None:
            self.bar["large"] += 1
        return found

    # --- минуты ---

    def close_minute(self):
        bar, self.bar = self.bar, None
        if bar is None:
            return []
        quiet = bar.pop("quiet", False)
        self.closed.append(bar)
        found = []
        volume = bar["volume"]
        sample = self.season_sample(bar["ts"])
        if len(sample) < SEASON_MIN:
            sample = list(self.recent)
        if len(sample) >= MIN_HISTORY and not quiet:
            z = robust_z(volume, sample)
            norm = statistics.median(sample)
            if z is not None and z >= VOLUME_Z and volume >= VOLUME_RATIO * max(norm, 1e-9):
                found.append(self._anomaly(
                    bar["ts"], "volume", sign(bar["buy"] - bar["sell"]), bar["close"], volume, z,
                    f"объём {fmt(volume)} при норме {fmt(norm)}: покупки {fmt(bar['buy'])}, продажи {fmt(bar['sell'])}",
                ))
        self.recent.append(volume)
        if bar["oi"] is not None:
            if self.last_oi is not None:
                move = bar["oi"] - self.last_oi
                if len(self.oi_moves) >= MIN_HISTORY and move and not quiet:
                    z = robust_z(move, list(self.oi_moves))
                    if z is not None and abs(z) >= OI_Z:
                        found.append(self._anomaly(
                            bar["ts"], "oi", sign(move), bar["close"], move, abs(z),
                            f"ОИ {move:+,.0f} за минуту, цена {bar['close'] - bar['open']:+g}: "
                            f"{_oi_meaning(move, bar['close'] - bar['open'])}".replace(",", " "),
                        ))
                self.oi_moves.append(move)
            self.last_oi = bar["oi"]
        return found

    def season_sample(self, ts):
        """Объёмы тех же минут суток ±SEASON_WINDOW в прошлые дни."""
        if not self.seasonal:
            return []
        center = ts.hour * 60 + ts.minute
        sample = []
        for offset in range(-SEASON_WINDOW, SEASON_WINDOW + 1):
            sample.extend(self.seasonal.get((center + offset) % 1440, ()))
        return sample

    def tick(self, now):
        """Закрыть по времени серию сделок и минуту, если новых сделок нет."""
        found = []
        if self.burst is not None and now - self.burst["last"] > SWEEP_GAP:
            found += self.finish_burst()
        if self.bar is not None and now.replace(second=0, microsecond=0) > self.bar["ts"]:
            found += self.close_minute()
        return found

    # --- стакан ---

    def on_book(self, rows, ts, replace=False, detect=True):
        if replace:
            self.book.replace(rows)
        else:
            self.book.apply(rows)
        return self._walls(ts) if detect else []

    def wall_limit(self):
        """Порог стены: выше 99,5-го процентиля уровней инструмента и в wall_ratio раз выше медианы сейчас."""
        asks, bids = self.book.top()
        sizes = [size for _price, size in asks + bids if size > 0]
        history = self.book_sizes.percentile(WALL_PCT)
        if len(sizes) < 6 or history is None:
            return None, None
        middle = statistics.median(sizes)
        return max(middle * self.wall_ratio, history), middle

    def _sample(self, ts):
        if self.sampled is not None and ts - self.sampled < WALL_SAMPLE_EVERY:
            return
        self.sampled = ts
        asks, bids = self.book.top(WALL_SAMPLE)
        for _price, size in asks + bids:
            if size > 0:
                self.book_sizes.add(size)

    def _near(self, side, price):
        """Сколько уровней той же стороны сейчас лучше этой цены."""
        levels = self.book.bids if side == "bid" else self.book.asks
        return sum(1 for level in levels if (level > price if side == "bid" else level < price))

    def _walls(self, ts):
        self._sample(ts)
        limit, middle = self.wall_limit()
        found = []
        if limit:
            asks, bids = self.book.top()
            for side, levels in (("ask", asks), ("bid", bids)):
                for price, size in levels:
                    if size < limit:
                        continue
                    wall = self.walls.get((side, price))
                    if wall is None:
                        if len(self.walls) >= MAX_WALLS:
                            self.walls.pop(next(iter(self.walls)))
                        self.walls[(side, price)] = {"peak": size, "since": ts, "traded": 0.0, "announced": False}
                        continue
                    wall["peak"] = max(wall["peak"], size)
                    if not wall["announced"] and ts - wall["since"] >= WALL_CONFIRM:
                        wall["announced"] = True
                        where = "продажу" if side == "ask" else "покупку"
                        found.append(self._anomaly(
                            ts, "wall", 1 if side == "bid" else -1, price, size, size / max(middle, 1e-9),
                            f"заявка на {where} {lots(size)}, в {size / max(middle, 1e-9):.0f} раз больше обычного уровня",
                        ))
        for key, wall in list(self.walls.items()):
            size = self.book.size_at(*key)
            if size >= WALL_GONE * wall["peak"]:
                continue
            del self.walls[key]
            if not wall["announced"]:
                continue
            life = ts - wall["since"]
            gone = wall["peak"] - size
            side, price = key
            seconds = int(life.total_seconds())
            if wall["traded"] >= WALL_EATEN * gone:
                found.append(self._anomaly(
                    ts, "eaten", -1 if side == "bid" else 1, price, wall["traded"], wall["traded"] / wall["peak"],
                    f"исполнено {fmt(wall['traded'])} из {fmt(wall['peak'])} за {seconds} с",
                ))
            elif self._near(side, price) <= WALL_NEAR:
                found.append(self._anomaly(
                    ts, "pulled", 1 if side == "bid" else -1, price, gone, gone / wall["peak"],
                    f"снято {fmt(gone)} из {fmt(wall['peak'])} через {seconds} с, исполнено {fmt(wall['traded'])}",
                ))
        return found

    def levels(self):
        """Верх стакана для страницы: [цена, объём, стена]."""
        asks, bids = self.book.top()
        def wall(side, price):
            found = self.walls.get((side, price))
            return bool(found and found["announced"])
        return (
            [[price, size, wall("ask", price)] for price, size in asks],
            [[price, size, wall("bid", price)] for price, size in bids],
        )

    def _anomaly(self, ts, kind, side, price, size, score, text):
        return {
            "symbol": self.symbol, "ts": ts, "kind": kind, "side": side,
            "price": price, "size": size, "score": round(float(score), 2), "text": text,
        }


def _oi_meaning(move, price_move):
    if move > 0:
        return "открывают лонги" if price_move > 0 else "открывают шорты" if price_move < 0 else "открывают позиции"
    return "закрывают шорты" if price_move > 0 else "закрывают лонги" if price_move < 0 else "закрывают позиции"


def _moscow_day(ts):
    return (ts + timedelta(hours=3)).date()
