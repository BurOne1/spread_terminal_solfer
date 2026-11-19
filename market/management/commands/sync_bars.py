"""5-минутки всех активных пар (вечный, спот и квартал в окне загрузки), часы — из них.

Из Finam берутся только TIME_FRAME_M5, окнами по WINDOW. Часы собираются из этих
5-минуток так же, как в онлайне (market.minutes.hours_from_minutes, сессия ФОРТС):
1ч и 5м всегда согласованы. Глубина ограничена: по умолчанию DEFAULT_DAYS дней,
не больше MAX_DAYS, --from старше границы обрезается. Глубже — sync_m5 / import_tv.
Уже записанные свечи не удаляются, повторный запуск обновляет те же 5-минутки и часы.
Неизвестный тикер пропускается, остальные инструменты пары всё равно пишутся.
Календарю и треугольнику на секондах квартал нужен на окно раньше своего:
там он второй. Инструмент, уже загруженный другой парой, второй раз не грузится.
У BRNYMEX в каждом окне две ноги: BR Мосбиржи и BZ NYMEX.
"""

from datetime import datetime, timedelta, timezone

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient, FinamError
from market.live import moex_trades
from market.minutes import WINDOW, hours_from_minutes, save_minutes
from market.models import Candle, Instrument, Pair
from strategy.rebuild import HISTORY_MONTHLY, HISTORY_QUARTERLY

DEFAULT_DAYS = 30
MAX_DAYS = 120


class Command(BaseCommand):
    help = "Загружает 5-минутки активных пар из Finam (не глубже --days) и собирает из них часы."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")
        parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                            help=f"не глубже N дней до конца, по умолчанию {DEFAULT_DAYS}, максимум {MAX_DAYS}")
        parser.add_argument("--from", dest="start", default="")
        parser.add_argument("--to", dest="end", default="")

    def handle(self, *args, **options):
        if not settings.FINAM_API_SECRET:
            raise CommandError("Нет FINAM_API_SECRET в окружении.")
        days = options["days"]
        if not 1 <= days <= MAX_DAYS:
            raise CommandError(f"--days от 1 до {MAX_DAYS}; глубже — sync_m5 или import_tv")
        end = _parse(options["end"]) if options["end"] else datetime.now(timezone.utc)
        floor = end - timedelta(days=days)
        start = _parse(options["start"]) if options["start"] else floor
        if end <= start:
            raise CommandError("конец не позже начала")
        if start < floor:
            self.stdout.write(f"--from раньше границы в {days} дн.: загрузка с {floor:%Y-%m-%d %H:%M}")
            start = floor

        pairs = Pair.objects.filter(active=True).exclude(venue="bybit").order_by("code")
        if options["pair"]:
            pairs = pairs.filter(code=options["pair"])
        if not pairs:
            raise CommandError("активных пар нет. Для евро сначала python manage.py add_eur")

        total = 0
        loaded = set()
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
                    if instrument.id in loaded:
                        continue
                    loaded.add(instrument.id)
                    got = self._instrument_bars(client, instrument, start, end, optional)
                    hours = _hours(instrument, start) if got else 0
                    total += got
                    self.stdout.write(f"  {instrument.code}: {got} свечей 5 мин, {hours} ч")
        finally:
            client.close()
        self.stdout.write(f"итого {total} свечей 5 мин. Дальше: python manage.py build_spread")

    def _instrument_bars(self, client, instrument, start, end, optional):
        count = 0
        for window_start, window_end in _windows(start, end):
            try:
                bars = client.bars(instrument.finam_symbol, window_start, window_end, "TIME_FRAME_M5")
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
            count += save_minutes(instrument, bars)
        return count


def _opened(instrument, start, span):
    """Начало загрузки: окно или история для аналитики (span до склейки), что раньше."""
    opened = instrument.listed_at or start
    return min(opened, instrument.roll_at - span) if instrument.roll_at else opened


def _brent(pair, start, end):
    """BR и BZ (и другие месячные ноги) окон, задевающих [start, end), с историей за два месяца."""
    legs = Instrument.objects.filter(
        pair=pair, kind__in=(Instrument.Kind.FUTURE, Instrument.Kind.FOREIGN), roll_at__gt=start,
    ).exclude(finam_symbol="")
    return [
        (leg, True) for leg in legs.order_by("roll_at", "kind")
        if _opened(leg, start, HISTORY_MONTHLY) < end
    ]


def _cross(pair, start, end):
    """Кварталы CME пары и ноги Мосбиржи тех же окон."""
    from market.cross import CROSS, legs, parse_code
    spec = CROSS[pair.code]
    windows = [target for target, _optional in _brent(pair, start, end)]
    symbols = set()
    for window in windows:
        a, _b, c = legs(spec, *parse_code(spec, window.code))
        symbols.update((a, c))
    moex = list(Instrument.objects.filter(finam_symbol__in=symbols))
    return [(item, True) for item in windows + moex]


def _ngnymex(pair, start, end):
    """Окна NYMEX пары и ноги NG Мосбиржи тех же окон.

    У NGNYMEX2 (base) — окна базы и следующее за ними: цены следующего окна.
    """
    from market.ngnymex import moex_code
    if pair.base:
        owner = _owner(pair.base)
        return _ngnymex(owner, start, end + timedelta(days=40)) if owner is not None else []
    windows = [target for target, _optional in _brent(pair, start, end) if target.kind == Instrument.Kind.FUTURE]
    moex = list(Instrument.objects.filter(code__in={moex_code(window.code) for window in windows}))
    return [(item, True) for item in windows + moex]


def _listed(pair, start, end):
    """Котировки календаря (NGX6NGZ6@RTSX): ног у него нет."""
    return [(item, True) for item, _optional in _brent(pair, start, end) if item.kind == Instrument.Kind.FUTURE]


def _quarters(owner, start, end, early=False):
    """Кварталы, чьё окно задевает [start, end). early — с окна предыдущего, там он секонд."""
    if owner is None:
        return []
    targets = []
    previous = None
    quarters = Instrument.objects.filter(pair=owner, kind=Instrument.Kind.FUTURE, roll_at__isnull=False)
    for instrument in quarters.order_by("roll_at"):
        opened = _opened(instrument, start, HISTORY_MONTHLY if owner.crypto else HISTORY_QUARTERLY)
        if early and previous is not None and previous.listed_at is not None:
            opened = min(opened, previous.listed_at)
        previous = instrument
        if instrument.finam_symbol and opened < end and instrument.roll_at > start:
            targets.append((instrument, True))
    return targets


def _owner(code):
    return Pair.objects.filter(code=code).first()


def _targets(pair, start, end):
    if pair.calendar:
        return _quarters(_owner(pair.base), start, end, early=True)
    if pair.nymex:
        return _brent(pair, start, end)
    if pair.listed:
        return _listed(pair, start, end)
    if pair.cross:
        return _cross(pair, start, end)
    if pair.spotfx:
        # Кварталы — у USD, CNY, EUR; спот USDCNY/EURUSD не из Finam: онлайн — run_live, история — import_tv.
        from market.spotfx import SPOTFX
        spec = SPOTFX[pair.code]
        return [target for code in (spec["a_owner"], spec["c_owner"]) for target in _quarters(_owner(code), start, end)]
    if pair.ngnymex:
        return _ngnymex(pair, start, end)
    if pair.foreign_source == "TRI":
        if pair.code in ("TRIGDS", "TRISVS"):
            # Кварталы GD/SV — у TRIGD/TRISV, SI — у USD, спот металла — у GOLD/SILVER.
            metal = "TRIGD" if pair.code == "TRIGDS" else "TRISV"
            return [target for code in (metal, "USD") for target in _quarters(_owner(code), start, end)]
        if pair.base:
            legs = ["TRIGD", "USD", "GOLD"] if pair.tri_code == "TRIGL" else [pair.tri_code, "USD"]
            return [target for code in legs for target in _quarters(_owner(code), start, end, early=True)]
        return _quarters(pair, start, end)
    targets = []
    perp = Instrument.objects.filter(pair=pair, code=pair.perp_code).first()
    if perp is None or not perp.finam_symbol:
        return targets
    targets.append((perp, False))
    if pair.spot_code != pair.perp_code:
        spot = Instrument.objects.filter(pair=pair, code=pair.spot_code).first()
        if spot is not None and spot.finam_symbol and spot.finam_symbol != perp.finam_symbol:
            targets.append((spot, True))
    return targets + _quarters(pair, start, end)


def _hours(instrument, start):
    """Часы из 5-минуток с start, только сессия ФОРТС. Час, начатый до start, — неполный, не трогается.
    Вернуть число часов в базе с первого записанного."""
    first = hours_from_minutes(instrument, start, keep=moex_trades, fetched_from=start)
    if first is None:
        return 0
    return Candle.objects.filter(instrument=instrument, ts__gte=first).count()


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
