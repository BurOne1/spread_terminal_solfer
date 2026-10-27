"""Часовые свечи всех активных пар: вечный, спот и квартал в окне загрузки.

Finam отдаёт TIME_FRAME_H1 примерно за 30 дней. Окна старше этого часто пустые.
Уже записанные свечи не удаляются, повторный запуск обновляет те же часы.
Неизвестный тикер пропускается, остальные инструменты пары всё равно пишутся.
"""

from datetime import datetime, timedelta, timezone

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient, FinamError
from market.models import Candle, Instrument, Pair

H1_DEPTH = timedelta(days=30)
WINDOW = timedelta(days=29)


class Command(BaseCommand):
    help = "Загружает часовые свечи активных пар из Finam."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")
        parser.add_argument("--from", dest="start", default="")
        parser.add_argument("--to", dest="end", default="")

    def handle(self, *args, **options):
        if not settings.FINAM_API_SECRET:
            raise CommandError("Нет FINAM_API_SECRET в окружении.")
        end = _parse(options["end"]) if options["end"] else datetime.now(timezone.utc)
        start = _parse(options["start"]) if options["start"] else end - H1_DEPTH
        if end <= start:
            raise CommandError("конец не позже начала")
        if start < end - H1_DEPTH:
            self.stdout.write(
                "Finam отдаёт час примерно за 30 дней. Более старые окна, скорее всего, будут пустыми. "
                "Строки, которые уже есть в базе, эта команда не стирает."
            )

        pairs = Pair.objects.filter(active=True).exclude(venue="bybit").order_by("code")
        if options["pair"]:
            pairs = pairs.filter(code=options["pair"])
        if not pairs:
            raise CommandError("активных пар нет. Для евро сначала python manage.py add_eur")

        total = 0
        try:
            client = FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL)
        except Exception as exc:
            raise CommandError(f"Finam не открылся: {exc}") from exc
        try:
            for pair in pairs:
                targets = _targets(pair, start, end)
                if not targets:
                    self.stdout.write(f"{pair.code}: нет инструментов, запустите python manage.py ensure_contracts --pair {pair.code}")
                    continue
                self.stdout.write(
                    f"{pair.code}: " + ", ".join(f"{item.code} ({item.finam_symbol})" for item, _optional in targets)
                )
                for instrument, optional in targets:
                    got = self._instrument_bars(client, instrument, start, end, optional)
                    total += got
                    self.stdout.write(f"  {instrument.code}: {got} свечей")
        finally:
            client.close()
        self.stdout.write(f"итого {total}")

    def _instrument_bars(self, client, instrument, start, end, optional):
        count = 0
        for window_start, window_end in _windows(start, end):
            try:
                bars = client.bars(instrument.finam_symbol, window_start, window_end, settings.BAR_TIMEFRAME)
            except FinamError as exc:
                text = str(exc)
                missing = exc.status in (400, 404) or "NOT_FOUND" in text or "doesn't exist" in text
                if missing:
                    self.stdout.write(f"  Finam не знает {instrument.finam_symbol}, пропуск")
                    return count
                if optional:
                    self.stdout.write(f"  {instrument.finam_symbol}: {exc}")
                    return count
                raise CommandError(str(exc)) from exc
            except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
                self.stderr.write(
                    f"  {instrument.finam_symbol}: сеть не ответила ({exc.__class__.__name__}), пропуск. "
                    "С этой машины Finam недоступен, свечи грузите на ВМ."
                )
                return count
            count += _save(instrument, bars)
        return count


def _targets(pair, start, end):
    if pair.foreign_source == "TRI":
        targets = []
        quarters = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at")
        for instrument in quarters:
            if not instrument.finam_symbol or instrument.roll_at is None:
                continue
            opened = instrument.listed_at or start
            if opened < end and instrument.roll_at > start:
                targets.append((instrument, True))
        return targets
    targets = []
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None or not perp.finam_symbol:
        return targets
    targets.append((perp, False))
    if pair.spot_code != pair.perp_code:
        spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
        if spot is not None and spot.finam_symbol and spot.finam_symbol != perp.finam_symbol:
            targets.append((spot, True))
    quarters = Instrument.objects.filter(pair=pair, kind=Instrument.Kind.FUTURE).order_by("roll_at")
    for instrument in quarters:
        if not instrument.finam_symbol or instrument.roll_at is None:
            continue
        opened = instrument.listed_at or start
        if opened < end and instrument.roll_at > start:
            targets.append((instrument, True))
    return targets


def _save(instrument, bars):
    if not bars:
        return 0
    Candle.objects.bulk_create(
        [Candle(instrument=instrument, **bar) for bar in bars],
        update_conflicts=True,
        unique_fields=["instrument", "ts"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    return len(bars)


def _windows(start, end):
    cursor = start
    while cursor < end:
        nxt = min(cursor + WINDOW, end)
        yield cursor, nxt
        cursor = nxt


def _parse(value: str) -> datetime:
    if len(value) == 10:
        day = datetime.fromisoformat(value)
        return day.replace(tzinfo=timezone.utc)
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
