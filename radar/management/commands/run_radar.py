"""Радар: стакан и лента Finam, детекторы аномалий. Отдельно от run_live."""

import asyncio

from django.core.management.base import BaseCommand

from radar.stream import run_forever


class Command(BaseCommand):
    help = "Слушает стакан и ленту Finam по ногам спредов и инструментам радара, ищет аномалии."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dump", type=int, default=0,
            help="напечатать столько первых сообщений сокета как есть, для проверки формата",
        )

    def handle(self, *args, **options):
        self.stdout.write("радар. Остановить: Ctrl+C")
        try:
            asyncio.run(run_forever(self.stdout.write, dump=options["dump"]))
        except KeyboardInterrupt:
            self.stdout.write("остановлен")
