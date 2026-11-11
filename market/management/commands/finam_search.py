"""Поиск инструментов Finam по части тикера, названия или биржи (MIC).

Список всех активных инструментов Finam берётся постранично из
/v1/assets/all и кладётся в var/finam_assets.json; повторный поиск идёт по
файлу без запросов. Файл старше суток или --refresh — скачать заново.

python manage.py finam_search NG                  # тикер, символ или название содержит «NG»
python manage.py finam_search NG --mic EDT        # и MIC содержит «EDT» (EDTS)
python manage.py finam_search brent               # по названию, регистр не важен
python manage.py finam_search --mic NYM           # всё на XNYM
python manage.py finam_search --mics              # какие MIC есть и сколько на них инструментов
python manage.py finam_search NG --mic RTSX --bars # и сколько часовых баров за 5 дней у найденных
"""

import json
import time
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient, FinamError

CACHE = settings.BASE_DIR / "var" / "finam_assets.json"
CACHE_AGE = timedelta(days=1)
# Лимит Finam — 200 запросов в минуту на метод.
PAGE_PAUSE = 0.31
MAX_PAGES = 5000
BARS_LIMIT = 60


def field(row, snake):
    """REST Finam отдаёт поля то в snake_case, то в camelCase."""
    if snake in row:
        return row[snake]
    head, *rest = snake.split("_")
    return row.get(head + "".join(part.title() for part in rest))


def download(client, log):
    assets, cursor, pages = [], 0, 0
    while pages < MAX_PAGES:
        page = client.all_assets(cursor=cursor)
        rows = page.get("assets") or []
        assets.extend({
            "symbol": field(row, "symbol") or "",
            "ticker": field(row, "ticker") or "",
            "mic": field(row, "mic") or "",
            "name": field(row, "name") or "",
            "type": field(row, "type") or "",
            "board": field(row, "board") or "",
        } for row in rows)
        pages += 1
        if pages % 20 == 0:
            log(f"  страниц {pages}, инструментов {len(assets)}")
        following = int(field(page, "next_cursor") or 0)
        if not rows or not following or following == cursor:
            break
        cursor = following
        time.sleep(PAGE_PAUSE)
    return assets


class Command(BaseCommand):
    help = "Ищет инструменты Finam по части тикера/названия и по части MIC."

    def add_arguments(self, parser):
        parser.add_argument("query", nargs="?", default="", help="часть тикера, символа или названия")
        parser.add_argument("--mic", default="", help="часть кода биржи: RTSX, EDT, NYM, XCME…")
        parser.add_argument("--type", default="", help="часть типа инструмента, например FUTURES")
        parser.add_argument("--mics", action="store_true", help="показать все MIC и число инструментов на них")
        parser.add_argument("--bars", action="store_true", help=f"проверить часовые бары у найденных (до {BARS_LIMIT})")
        parser.add_argument("--refresh", action="store_true", help="скачать список заново")
        parser.add_argument("--limit", type=int, default=200, help="сколько строк печатать")

    def handle(self, *args, **options):
        assets = self.assets(options["refresh"])
        if options["mics"]:
            counts = {}
            for row in assets:
                counts[row["mic"]] = counts.get(row["mic"], 0) + 1
            for mic, count in sorted(counts.items(), key=lambda item: -item[1]):
                self.stdout.write(f"{mic or '(пусто)':10} {count}")
            return
        query, mic, kind = options["query"].lower(), options["mic"].lower(), options["type"].lower()
        if not (query or mic or kind):
            raise CommandError("Задайте часть тикера, --mic или --type. Список бирж: --mics")
        found = [
            row for row in assets
            if (not query or query in row["symbol"].lower() or query in row["ticker"].lower() or query in row["name"].lower())
            and (not mic or mic in row["mic"].lower())
            and (not kind or kind in row["type"].lower())
        ]
        found.sort(key=lambda row: (row["mic"], row["ticker"]))
        self.stdout.write(f"найдено {len(found)}" + (f", показаны первые {options['limit']}" if len(found) > options["limit"] else ""))
        bars = self.bars(found[:BARS_LIMIT]) if options["bars"] else {}
        for row in found[:options["limit"]]:
            extra = f"  баров {bars[row['symbol']]}" if row["symbol"] in bars else ""
            self.stdout.write(f"{row['symbol']:26} {row['type']:16} {row['name'][:60]}{extra}")
        if options["bars"] and len(found) > BARS_LIMIT:
            self.stdout.write(f"бары проверены у первых {BARS_LIMIT}: сузьте поиск, чтобы проверить остальные")

    def client(self):
        if not settings.FINAM_API_SECRET:
            raise CommandError("Нет FINAM_API_SECRET в окружении.")
        return FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL)

    def assets(self, refresh):
        fresh = CACHE.exists() and datetime.now() - datetime.fromtimestamp(CACHE.stat().st_mtime) < CACHE_AGE
        if fresh and not refresh:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        self.stdout.write("скачиваю список инструментов Finam, это один раз в сутки…")
        try:
            with self.client() as client:
                assets = download(client, self.stdout.write)
        except FinamError as exc:
            if CACHE.exists():
                self.stdout.write(f"Finam не отдал список ({exc}), ищу по прошлой копии")
                return json.loads(CACHE.read_text(encoding="utf-8"))
            raise CommandError(f"Finam не отдал список инструментов: {exc}") from exc
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(assets, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(f"сохранено {len(assets)} инструментов в {CACHE}")
        return assets

    def bars(self, rows):
        now = datetime.now(timezone.utc)
        found = {}
        with self.client() as client:
            for row in rows:
                try:
                    found[row["symbol"]] = len(client.bars(row["symbol"], now - timedelta(days=5), now, "TIME_FRAME_H1"))
                except FinamError as exc:
                    found[row["symbol"]] = f"ошибка {exc.status}"
        return found
