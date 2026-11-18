"""Ряд spread_rate, открытая позиция и закрытые бумажные сделки."""

from django.db import models

from market.models import Instrument, Pair


class SpreadBar(models.Model):
    pair = models.ForeignKey(Pair, null=True, blank=True, on_delete=models.PROTECT, related_name="spread_bars")
    ts = models.DateTimeField()
    contract = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="spread_bars")
    future_close = models.DecimalField(max_digits=18, decimal_places=6)
    perp_close = models.DecimalField(max_digits=18, decimal_places=6)
    spot = models.DecimalField(max_digits=18, decimal_places=6)
    raw_spread = models.DecimalField(max_digits=18, decimal_places=6)
    days_left = models.PositiveIntegerField()
    ruonia = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    foreign_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    fair = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    spread_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    # Те же величины по ценам открытия часа. (O+C)/2 считается из двух вариантов.
    raw_open = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    rate_open = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    class Meta:
        verbose_name = "бар спреда"
        verbose_name_plural = "бары спреда"
        ordering = ["pair", "ts"]
        constraints = [
            models.UniqueConstraint(fields=["pair", "ts"], name="uniq_spreadbar_pair_ts"),
        ]

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} {self.raw_spread} → {self.spread_rate}"


class SpreadBarM5(models.Model):
    """Бар спреда на 5 минут: те же поля, что у SpreadBar, ts — начало 5-минутки (UTC).

    Строится из MinuteCandle тем же пересчётом (strategy/rebuild.py, frame="M5").
    15 минут собираются из этих баров при запросе, отдельно не хранятся.
    """

    pair = models.ForeignKey(Pair, null=True, blank=True, on_delete=models.PROTECT, related_name="spread_bars_m5")
    ts = models.DateTimeField()
    contract = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="spread_bars_m5")
    future_close = models.DecimalField(max_digits=18, decimal_places=6)
    perp_close = models.DecimalField(max_digits=18, decimal_places=6)
    spot = models.DecimalField(max_digits=18, decimal_places=6)
    raw_spread = models.DecimalField(max_digits=18, decimal_places=6)
    days_left = models.PositiveIntegerField()
    ruonia = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    foreign_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    fair = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    spread_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    raw_open = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    rate_open = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    class Meta:
        verbose_name = "бар спреда 5 мин"
        verbose_name_plural = "бары спреда 5 мин"
        ordering = ["pair", "ts"]
        constraints = [
            models.UniqueConstraint(fields=["pair", "ts"], name="uniq_spreadbar_m5_pair_ts"),
        ]

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} M5 {self.raw_spread} → {self.spread_rate}"


BAR_FIELDS = (
    "pair_id", "ts", "contract_id", "future_close", "perp_close", "spot", "raw_spread", "days_left",
    "ruonia", "foreign_rate", "fair", "spread_rate", "raw_open", "rate_open",
)


class PositionState(models.Model):
    name = models.CharField(max_length=32, unique=True, default="spread_rate")
    side = models.SmallIntegerField(default=0)
    size = models.PositiveSmallIntegerField(default=0)
    avg_spread = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    open_fund = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    realized = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    used_levels = models.JSONField(default=list)
    tp_used = models.JSONField(default=list)
    no_avg = models.BooleanField(default=False)
    touched_zero = models.BooleanField(default=False)
    contract_code = models.CharField(max_length=16, blank=True)
    entry_ts = models.DateTimeField(null=True, blank=True)
    entry_spread = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    entry_legs = models.JSONField(default=list)

    class Meta:
        verbose_name = "позиция"
        verbose_name_plural = "позиции"

    def __str__(self):
        return f"{self.name} side={self.side} size={self.size}"


class EquityPoint(models.Model):
    pair = models.ForeignKey(Pair, on_delete=models.CASCADE, related_name="equity")
    ts = models.DateTimeField()
    contract = models.CharField(max_length=16)
    equity = models.DecimalField(max_digits=18, decimal_places=2)
    marker = models.CharField(max_length=8, blank=True)

    class Meta:
        verbose_name = "точка PNL"
        verbose_name_plural = "точки PNL"
        ordering = ["ts"]
        constraints = [
            models.UniqueConstraint(fields=["pair", "ts"], name="uniq_equity_pair_ts"),
        ]


class PaperTrade(models.Model):
    class Side(models.TextChoices):
        LONG = "LONG", "лонг"
        SHORT = "SHORT", "шорт"

    class Reason(models.TextChoices):
        TP = "tp", "тейк"
        ROLL = "roll", "склейка"
        SIGN = "sign", "смена знака"

    side = models.CharField(max_length=8, choices=Side.choices)
    entry_ts = models.DateTimeField(null=True, blank=True)
    exit_ts = models.DateTimeField()
    n = models.PositiveSmallIntegerField()
    reason = models.CharField(max_length=8, choices=Reason.choices)
    entry_spread = models.DecimalField(max_digits=18, decimal_places=6)
    exit_spread = models.DecimalField(max_digits=18, decimal_places=6)
    entry_legs = models.JSONField(default=list)
    exit_future = models.DecimalField(max_digits=18, decimal_places=6)
    exit_perp = models.DecimalField(max_digits=18, decimal_places=6)
    price = models.DecimalField(max_digits=18, decimal_places=4)
    fund = models.DecimalField(max_digits=18, decimal_places=4)
    fee = models.DecimalField(max_digits=18, decimal_places=4)
    pnl = models.DecimalField(max_digits=18, decimal_places=4)
    notional = models.DecimalField(max_digits=18, decimal_places=4)
    pnl_pct = models.DecimalField(max_digits=12, decimal_places=6)

    class Meta:
        verbose_name = "сделка"
        verbose_name_plural = "сделки"
        ordering = ["exit_ts"]

    def __str__(self):
        return f"{self.side} {self.entry_ts} → {self.exit_ts} {self.pnl}"