"""Только TRISVS: SV + SI − SLVRUB_TOM (TRISV на споте серебра, без фандинга).

Нужны пары TRISV и SILVER. Спот серебра SILVER переводится на SLVRUB_TOM@MISX,
если там был вечный. Дальше: sync_bars --pair SILVER, build_spread --pair TRISVS,
перезапуск run_live.
"""

from django.core.management.base import BaseCommand

from market.management.commands.add_extra_spreads import Command as Extra


class Command(BaseCommand):
    help = "Добавляет треугольник серебра на споте SLVRUB_TOM (TRISVS)."

    def handle(self, *args, **options):
        extra = Extra(stdout=self.stdout, stderr=self.stderr)
        extra.add_trisvs()
        self.stdout.write("Дальше: sync_bars --pair SILVER, build_spread --pair TRISVS и перезапуск run_live.")
