"""Срочный − вечный на криптоиндексы Мосбиржи: BT − BTCUSDF и EH − ETHUSDF.

Оба фьючерса — на один индекс (MOEXBTC, MOEXETH), котировка в долларах за 1
монету, расчёты в рублях. Срочные месячные: последний день торгов — последняя
пятница месяца, склейка в 18:00 МСК (market/roll.py: crypto_month_points).
Очищенный спред равен сырому (fair = 0). Стратегия учитывает фандинг
вечного — SWAPRATE с ISS (sync_funding, ряды BTC и ETH).
Спот — сам вечный. Уровни — доли цены вечного.

python manage.py add_crypto, затем sync_bars и build_spread по каждой паре.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from market.models import Pair


def levels(pct, tp):
    return {
        "long_levels": {"signed": True, "values": [-pct]},
        "short_levels": {"signed": True, "values": [pct]},
        "long_tp": {"signed": True, "values": [tp]},
        "short_tp": {"signed": True, "values": [-tp]},
    }


SPECS = (
    {"code": "BTCMOEX", "title": "BT − BTCUSDF", "root": "BT", "perp": "BTCUSDF"},
    {"code": "ETHMOEX", "title": "EH − ETHUSDF", "root": "EH", "perp": "ETHUSDF"},
)


class Command(BaseCommand):
    help = "Добавляет пары «месячный − вечный» на биткоин и эфир Мосбиржи и заводит их контракты."

    def handle(self, *args, **options):
        for spec in SPECS:
            symbol = f"{spec['perp']}@RTSX"
            defaults = {
                "title": spec["title"],
                "future_root": spec["root"],
                "perp_code": spec["perp"],
                "perp_symbol": symbol,
                "spot_code": spec["perp"],
                "spot_symbol": symbol,
                "mic": "RTSX",
                "foreign_source": "CRYPTO",
                "quote": "USD",
                "lot": 1,
                "future_scale": 1,
                "active": True,
            }
            pair, created = Pair.objects.get_or_create(code=spec["code"], defaults={**defaults, **levels(0.005, 0.001)})
            if not created:
                # Уровни, которые уже правили, не трогаем.
                Pair.objects.filter(pk=pair.pk).update(**defaults)
            mark = "создана" if created else "обновлена"
            self.stdout.write(f"пара {spec['code']} {mark}: {spec['title']}, месячные {spec['root']}, справедливый 0, $")
            call_command("ensure_contracts", pair=spec["code"])
        self.stdout.write("Дальше: python manage.py sync_funding --from 2025-01-01 --only BTC,ETH,")
        self.stdout.write("sync_bars --pair BTCMOEX и --pair ETHMOEX,")
        self.stdout.write("потом build_spread --pair BTCMOEX и --pair ETHMOEX. Онлайн-поток — перезапуск run_live.")
