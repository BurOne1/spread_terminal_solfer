"""Переносит ноги NG Мосбиржи (NGF2025, NGZ2026…) к паре NGNYMEX и ставит им окна.

Раньше календарь NGCAL заводил ноги у себя, а NGNYMEX — у себя без окна,
и после import_tv они оказывались вперемешку. Теперь хозяин один — NGNYMEX,
окно то же, что у её окна NYMEX; календарь находит фронт по коду.

python manage.py fix_ng_legs --dry-run
python manage.py fix_ng_legs
"""

import re
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from market import ngnymex
from market.models import Instrument, Pair

LEG = re.compile(r"^NG[FGHJKMNQUVXZ]20\d\d$")


class Command(BaseCommand):
    help = "Ноги NG Мосбиржи — к паре NGNYMEX с окнами, затем пересчёт NGNYMEX и NGCAL."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="только показать")

    def handle(self, *args, **options):
        pair = Pair.objects.filter(foreign_source="NGNYM").first()
        if pair is None:
            raise CommandError("нет пары NGNYMEX: python manage.py add_extra_spreads")
        legs = sorted(
            (item for item in Instrument.objects.filter(code__startswith="NG").select_related("pair")
             if LEG.match(item.code)),
            key=lambda item: (item.code[-4:], ngnymex.MONTHS.index(item.code[2])),
        )
        if not legs:
            self.stdout.write("ног NG Мосбиржи нет")
            return
        moving = [item for item in legs if item.pair_id != pair.id or item.roll_at is None]
        by_owner = {}
        for item in moving:
            key = item.pair.code if item.pair else "—"
            by_owner.setdefault(key, []).append(item.code + ("" if item.roll_at else " (без окна)"))
        self.stdout.write(f"ног NG Мосбиржи {len(legs)}, перенести к {pair.code}: {len(moving)}")
        for owner, codes in by_owner.items():
            self.stdout.write(f"  у {owner}: {', '.join(codes)}")
        if options["dry_run"]:
            self.stdout.write("проверка без записи")
            return
        first, last = int(legs[0].code[-4:]), int(legs[-1].code[-4:])
        with transaction.atomic():
            # Окна NYMEX за все годы ног; moex_leg переносит ногу и ставит ей окно.
            rows = ngnymex.ensure(
                pair, datetime(first, 1, 1, tzinfo=timezone.utc), datetime(last, 12, 1, tzinfo=timezone.utc), update=True,
            )
        self.stdout.write(f"окон {len(rows)}, ноги у {pair.code}")
        from analytics.history import refresh
        from strategy.rebuild import rebuild_spread
        for code in ("NGNYMEX", "NGCAL"):
            target = Pair.objects.filter(code=code, active=True).first()
            if target is None:
                continue
            self.stdout.write(f"пересчитано {code}: спред {rebuild_spread(code)}, аналитика {refresh(target)}")
