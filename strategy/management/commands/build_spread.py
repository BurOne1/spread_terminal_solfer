"""Пишет strategy_spreadbar из свечей и ставок, которые уже в базе."""

from django.core.management.base import BaseCommand, CommandError

from strategy.rebuild import rebuild_spread


class Command(BaseCommand):
    help = "Считает сырой спред и spread_rate выбранной пары или всех активных."

    def add_arguments(self, parser):
        parser.add_argument("--pair", default="", help="код пары, пусто — все активные")
        parser.add_argument("--frame", default="H1", choices=["H1", "M5"],
                            help="H1 — часы (по умолчанию), M5 — 5 минут из MinuteCandle")

    def handle(self, *args, **options):
        frame = options["frame"]
        count = rebuild_spread(options["pair"], frame=frame)
        if not count:
            raise CommandError("нечего считать: нет вечного, квартала или общих часов")
        self.stdout.write(f"баров {count}" + (" (5 мин)" if frame == "M5" else ""))
        if frame == "M5":
            # Аналитика строится по часам: 5-минутки её не меняют.
            return
        from analytics.history import refresh
        from market.models import Pair
        pairs = Pair.objects.filter(active=True)
        if options["pair"]:
            pairs = pairs.filter(code=options["pair"])
        for pair in pairs:
            self.stdout.write(f"  аналитика {pair.code}: баров {refresh(pair)}")