"""Пересчитывает таблицу аналитики (analytics_historybar) из свечей."""

from django.core.management.base import BaseCommand, CommandError

from analytics.history import refresh
from market.models import Pair


class Command(BaseCommand):
    help = "История контрактов для /analytics/: два месяца у месячных, два квартала у квартальных."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")

    def handle(self, *args, **options):
        pairs = Pair.objects.filter(active=True).order_by("code")
        if options["pair"]:
            pairs = pairs.filter(code=options["pair"])
        if not pairs:
            raise CommandError("нет такой активной пары")
        for pair in pairs:
            self.stdout.write(f"{pair.code}: баров {refresh(pair)}")
