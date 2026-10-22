"""Догружает фандинг CNYRUBF с Мосбиржи."""

from django.core.management.base import BaseCommand

from rates.funding import fill_funding


class Command(BaseCommand):
    help = "Добирает недостающий фандинг CNYRUBF с ISS Мосбиржи."

    def handle(self, *args, **options):
        result = fill_funding()
        last = result["last"]
        text = "нет" if last is None else f"{last[0]} = {last[1]} руб/контракт"
        self.stdout.write(
            f"фандинг {result['from']} → {result['to']}: {result['days']} дней, последний {text}"
        )
