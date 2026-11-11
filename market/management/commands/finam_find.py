"""Какие фьючерсы CME, NYMEX, COMEX есть у Finam: перебор тикеров и запрос баров.

Полный список /v1/assets Finam по REST не отдаёт (ответ слишком большой),
поэтому для каждого корня берутся ближайшие месяцы в формате Finam
(BZF27@XNYM: корень, буква месяца, две цифры года) и запрашиваются часовые
бары за последние дни. Есть бары — котировки есть; число баров показывает,
насколько инструмент живой.

python manage.py finam_find                      # известные корни всех трёх бирж
python manage.py finam_find --mic XNYM           # только NYMEX
python manage.py finam_find --roots CL,BZ,QM --mic XNYM --months 6
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.finam import FinamClient, FinamError

MONTHS = "FGHJKMNQUVXZ"
# Корни по биржам Finam. XCBT (зерно CBOT) в списке бирж Finam нет.
ROOTS = {
    "XNYM": {
        "CL": "WTI", "BZ": "Brent Last Day", "NG": "газ Henry Hub", "RB": "бензин RBOB",
        "HO": "дизель ULSD", "QM": "WTI mini", "QG": "газ mini", "PL": "платина", "PA": "палладий",
    },
    "XCEC": {"GC": "золото", "SI": "серебро", "HG": "медь", "MGC": "золото micro", "SIL": "серебро 1000"},
    "XCME": {
        "ES": "S&P 500 E-mini", "NQ": "Nasdaq 100 E-mini", "RTY": "Russell 2000", "MES": "S&P micro",
        "MNQ": "Nasdaq micro", "6E": "евро", "6J": "иена", "6B": "фунт", "6C": "канадский доллар",
        "6A": "австралийский доллар", "6S": "франк", "6M": "песо", "LE": "живой скот", "HE": "свиньи",
        "BTC": "биткоин", "ETH": "эфир",
    },
}


def candidates(root, mic, now, count):
    """Ближайшие count месяцев: ZW26 и т. п. — с текущего месяца."""
    year, month = now.year, now.month
    for _ in range(count):
        yield f"{root}{MONTHS[month - 1]}{year % 100:02d}@{mic}"
        month += 1
        if month > 12:
            year, month = year + 1, 1


class Command(BaseCommand):
    help = "Проверяет, какие фьючерсы CME/NYMEX/COMEX отдаёт Finam, по часовым барам ближайших месяцев."

    def add_arguments(self, parser):
        parser.add_argument("--mic", default="", help="XNYM, XCEC или XCME через запятую; пусто — все три")
        parser.add_argument("--roots", default="", help="свои корни через запятую, например CL,BZ")
        parser.add_argument("--months", type=int, default=4, help="сколько ближайших месяцев пробовать")
        parser.add_argument("--days", type=int, default=5, help="за сколько дней считать бары")

    def handle(self, *args, **options):
        if not settings.FINAM_API_SECRET:
            raise CommandError("Нет FINAM_API_SECRET в окружении.")
        mics = [mic.strip().upper() for mic in options["mic"].split(",") if mic.strip()] or list(ROOTS)
        own = [root.strip() for root in options["roots"].split(",") if root.strip()]
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=options["days"])
        found = 0
        with FinamClient(settings.FINAM_API_SECRET, settings.FINAM_BASE_URL) as client:
            for mic in mics:
                roots = {root: "" for root in own} if own else ROOTS.get(mic, {})
                self.stdout.write(f"\n{mic}")
                for root, title in roots.items():
                    hits = []
                    for symbol in candidates(root, mic, now, options["months"]):
                        try:
                            bars = client.bars(symbol, start, now, "TIME_FRAME_H1")
                        except FinamError as exc:
                            if exc.status in (400, 404) or "doesn't exist" in str(exc):
                                continue
                            hits.append(f"{symbol}: ошибка {exc.status}")
                            continue
                        last = f", последняя {bars[-1]['close']}" if bars else ""
                        hits.append(f"{symbol}: баров {len(bars)}{last}")
                    name = f"{root} ({title})" if title else root
                    if hits:
                        found += 1
                        self.stdout.write(f"  {name}")
                        for hit in hits:
                            self.stdout.write(f"    {hit}")
                    else:
                        self.stdout.write(f"  {name}: нет")
        self.stdout.write(f"\nнайдено корней: {found}. «баров 0» — инструмент есть, но за эти дни не торговался.")
