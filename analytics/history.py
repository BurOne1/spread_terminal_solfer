"""Заполнение analytics_historybar из свечей.

refresh — пересчитать пару целиком. Страница сама обновляет пару, если её
SpreadBar ушёл вперёд больше чем на STALE: так идущий контракт дорисовывается
раз в день, а не на каждый запрос. После import_tv и правки контрактов —
python manage.py build_analytics.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Max

from analytics.models import HistoryBar
from strategy.models import SpreadBar
from strategy.rebuild import history_bars

STALE = timedelta(days=1)


def refresh(pair):
    """Пересчитать историю пары. Вернуть число баров."""
    rows = [
        HistoryBar(
            pair=pair, contract_id=bar.contract_id, ts=bar.ts, days_left=bar.days_left,
            raw_spread=bar.raw_spread, spread_rate=bar.spread_rate, spot=bar.spot,
        )
        for bar in history_bars(pair)
        if bar.raw_spread is not None
    ]
    with transaction.atomic():
        HistoryBar.objects.filter(pair=pair).delete()
        HistoryBar.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def stale(pair):
    """Истории нет или SpreadBar пары ушёл вперёд больше чем на STALE."""
    last = HistoryBar.objects.filter(pair=pair).aggregate(ts=Max("ts"))["ts"]
    if last is None:
        return True
    spread = SpreadBar.objects.filter(pair=pair).aggregate(ts=Max("ts"))["ts"]
    return spread is not None and spread > last + STALE
