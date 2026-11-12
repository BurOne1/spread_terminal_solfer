"""Догружает фандинг вечных фьючерсов с Мосбиржи: валюта, золото, серебро.

Фандинг не зависит от свечей в базе: SWAPRATE берётся с ISS по коду вечника.
--from 2023-01-01 перезагружает ряды целиком (с запуска каждого вечника),
--only USD,EUR — только эти ряды (CNY, USD, EUR, GOLD, SILVER).
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from rates.funding import SERIES, fill_funding

ALIASES = {
    "CNY": "swaprate_rub",
    "USD": "swaprate_usd",
    "EUR": "swaprate_eur",
    "GOLD": "swaprate_gold",
    "SILVER": "swaprate_silver",
}


class Command(BaseCommand):
    help = "Добирает недостающий фандинг вечных с ISS Мосбиржи."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="since", help="перезагрузить с даты YYYY-MM-DD, например 2023-01-01")
        parser.add_argument("--only", help="ряды через запятую: CNY, USD, EUR, GOLD, SILVER")

    def handle(self, *args, **options):
        since = None
        if options["since"]:
            try:
                since = date.fromisoformat(options["since"])
            except ValueError as error:
                raise CommandError(f"--from: нужна дата YYYY-MM-DD, получено {options['since']!r}") from error
        fields = None
        if options["only"]:
            names = [name.strip().upper() for name in options["only"].split(",") if name.strip()]
            unknown = [name for name in names if name not in ALIASES]
            if unknown:
                raise CommandError(f"--only: неизвестные ряды {', '.join(unknown)}; есть {', '.join(ALIASES)}")
            fields = {ALIASES[name] for name in names}
        result = fill_funding(since=since, fields=fields)
        last = result["last"]
        text = "нет" if last is None else f"{last[0]} = {last[1]} руб/контракт"
        self.stdout.write(
            f"фандинг {result['from']} → {result['to']}: {result['days']} дней, последний {text}"
        )
        from rates.models import FundingDay
        from django.db.models import Count, Max, Min
        for name, field in ALIASES.items():
            if field not in SERIES or (fields and field not in fields):
                continue
            row = FundingDay.objects.filter(**{f"{field}__isnull": False}).aggregate(
                first=Min("date"), last=Max("date"), days=Count("id"))
            self.stdout.write(f"  {name}: {row['first']} → {row['last']}, дней {row['days']}")
