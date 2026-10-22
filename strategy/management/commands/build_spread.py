"""Пишет strategy_spreadbar из свечей и ставок, которые уже в базе."""

from django.core.management.base import BaseCommand, CommandError

from strategy.rebuild import rebuild_spread


class Command(BaseCommand):
    help = "Считает сырой спред и spread_rate выбранной пары или всех активных."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")

    def handle(self, *args, **options):
        count = rebuild_spread(options["pair"])
        if not count:
            raise CommandError("нечего считать: нет вечного, квартала или общих часов")
        self.stdout.write(f"баров {count}")