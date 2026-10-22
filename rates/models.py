"""Дневные ставки и фандинг. На бар попадают через forward-fill, как align_rate."""

from django.db import models


class RateDay(models.Model):
    date = models.DateField(unique=True)
    ruonia = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    foreign_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    foreign_source = models.CharField(max_length=32, default="SHIBOR")
    shibor = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    dfedtaru = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    estr = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)

    class Meta:
        verbose_name = "ставка"
        verbose_name_plural = "ставки"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date:%Y-%m-%d} RUONIA={self.ruonia} {self.foreign_source}={self.foreign_rate}"


class FundingDay(models.Model):
    date = models.DateField(unique=True)
    swaprate_rub = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    swaprate_usd = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    swaprate_eur = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    swaprate_gold = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    source = models.CharField(max_length=32, default="moex")

    class Meta:
        verbose_name = "фандинг"
        verbose_name_plural = "фандинг"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date:%Y-%m-%d} {self.swaprate_rub}"


class FundingPoint(models.Model):
    """Ставка фандинга площадки, не дневной SWAPRATE Мосбиржи.

    У Bybit это 8-часовая ставка в долях (0.0001 = 0.01% за интервал).
    """

    pair = models.ForeignKey("market.Pair", on_delete=models.CASCADE, related_name="funding_points")
    ts = models.DateTimeField()
    rate = models.DecimalField(max_digits=18, decimal_places=10)
    source = models.CharField(max_length=32, default="bybit")

    class Meta:
        verbose_name = "точка фандинга"
        verbose_name_plural = "точки фандинга"
        ordering = ["pair", "ts"]
        constraints = [
            models.UniqueConstraint(fields=["pair", "ts"], name="uniq_funding_point"),
        ]

    def __str__(self):
        return f"{self.pair_id} {self.ts:%Y-%m-%d %H:%M} {self.rate}"