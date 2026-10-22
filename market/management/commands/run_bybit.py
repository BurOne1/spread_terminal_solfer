"""Держит сокет Bybit: сначала догрузка разрывов, потом живые бары."""

import asyncio

from django.core.management.base import BaseCommand

from market.bybit_live import run_forever


class Command(BaseCommand):
    help = "Догружает дыры и слушает Bybit WebSocket. Финам не трогает."

    def handle(self, *args, **options):
        self.stdout.write("живой сокет Bybit. Остановить: Ctrl+C")
        asyncio.run(run_forever(self.stdout.write))
