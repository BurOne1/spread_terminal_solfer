
"""Держит сокет Finam: сначала догрузка разрывов, потом живые бары."""

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.live import run_forever


class Command(BaseCommand):
    help = "Проверяет разрывы в свечах, догружает их и слушает Finam WebSocket."

    def handle(self, *args, **options):
        if not settings.FINAM_API_SECRET:
            raise CommandError("Нет FINAM_API_SECRET в окружении.")
        self.stdout.write("живой сокет Finam. Остановить: Ctrl+C")
        asyncio.run(run_forever(self.stdout.write))