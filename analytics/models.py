"""Ряд аналитики: спред каждого контракта за всю его историю, а не только окно.

У месячных контрактов — два месяца до склейки, у квартальных — два квартала
(strategy.rebuild.history_bars). Окна соседних контрактов перекрываются,
поэтому ключ — (пара, контракт, час), а не (пара, час), как у SpreadBar.
"""

from django.db import models

from market.models import Instrument, Pair


class HistoryBar(models.Model):
    pair = models.ForeignKey(Pair, on_delete=models.CASCADE, related_name="history_bars")
    contract = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="history_bars")
    ts = models.DateTimeField()
    days_left = models.PositiveIntegerField()
    raw_spread = models.DecimalField(max_digits=18, decimal_places=6)
    spread_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    spot = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    class Meta:
        verbose_name = "бар аналитики"
        verbose_name_plural = "бары аналитики"
        constraints = [
            models.UniqueConstraint(fields=["pair", "contract", "ts"], name="uniq_historybar_pair_contract_ts"),
        ]
        indexes = [models.Index(fields=["pair", "ts"], name="historybar_pair_ts")]

    def __str__(self):
        return f"{self.contract_id} {self.ts:%Y-%m-%d %H:%M} {self.raw_spread}"
