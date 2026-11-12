"""Инструменты и часовые свечи. Сырьё для спреда, без расчёта стратегии."""

from django.db import models


class Pair(models.Model):
    """Торгуемая пара: юань сейчас, доллар потом.

    Кварталы, вечный и спот живут в Instrument и ссылаются сюда.
    Новая пара — строка здесь, не новая таблица.
    """

    code = models.CharField(max_length=8, unique=True)
    title = models.CharField(max_length=64)
    future_root = models.CharField(max_length=8)
    perp_code = models.CharField(max_length=16)
    perp_symbol = models.CharField(max_length=64)
    spot_code = models.CharField(max_length=16)
    spot_symbol = models.CharField(max_length=64)
    mic = models.CharField(max_length=8, default="RTSX")
    foreign_source = models.CharField(max_length=32, default="SHIBOR")
    venue = models.CharField(max_length=16, default="finam")
    quote = models.CharField(max_length=8, default="RUB")
    lot = models.PositiveIntegerField(default=1000)
    future_scale = models.PositiveIntegerField(default=1)
    # Чьи кварталы берёт пара. Календарь (foreign_source CAL) — секонд минус
    # фронт базы, треугольник на секондах — тот же треугольник базы на вторых
    # контрактах. Пусто — пара на своих инструментах.
    base = models.CharField(max_length=8, blank=True)
    long_levels = models.JSONField(default=list)
    short_levels = models.JSONField(default=list)
    long_tp = models.JSONField(default=list)
    short_tp = models.JSONField(default=list)
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "пара"
        verbose_name_plural = "пары"
        ordering = ["code"]

    def __str__(self):
        return self.code

    @property
    def calendar(self):
        return self.foreign_source == "CAL"

    @property
    def nymex(self):
        """BR Мосбиржи минус BZ NYMEX, оба месячные, см. market/brent.py."""
        return self.foreign_source == "NYMEX"

    @property
    def cross(self):
        """CR × CNH − Si, Si × 6E − Eu: третья нога с CME, см. market/cross.py.

        Только пары из market.cross.CROSS: у своих пар тип может называться похоже.
        """
        from market.cross import CROSS
        return self.foreign_source == "CMEFX" and self.code in CROSS

    @property
    def ngnymex(self):
        """NG Мосбиржи минус NG NYMEX, см. market/ngnymex.py."""
        return self.foreign_source == "NGNYM"

    @property
    def listed(self):
        """Готовый спред биржи одним инструментом, см. market/ngcal.py."""
        return self.foreign_source == "LISTED"

    @property
    def absolute_levels(self):
        """Уровни в единицах спреда, а не в долях цены: готовый календарь NGCAL."""
        return self.listed

    @property
    def zero_fair(self):
        """Справедливое значение ноль: spread_rate = raw, фандинга нет."""
        return self.nymex or self.cross or self.listed or self.ngnymex

    @property
    def tri_code(self):
        """TRIGD, TRISV или TRIGL. У треугольника на секондах — его база."""
        return self.base or self.code

    @property
    def quarters_code(self):
        """Пара, чьи кварталы задают окна. У TRIGL свои ноги чужие, окна — GL из GOLD.
        У TRIGDS — GD из TRIGD: тот же треугольник, только третья нога спот."""
        code = self.base or self.code
        return {"TRIGL": "GOLD", "TRIGDS": "TRIGD"}.get(code, code)


class Instrument(models.Model):
    class Kind(models.TextChoices):
        PERP = "perp", "вечный"
        FUTURE = "future", "квартальный"
        SPOT = "spot", "спот"
        # Нога с другой биржи в том же окне, что и квартал пары: BZ NYMEX у BR.
        FOREIGN = "foreign", "фьючерс другой биржи"

    pair = models.ForeignKey(Pair, null=True, blank=True, on_delete=models.PROTECT, related_name="instruments")
    code = models.CharField(max_length=16, unique=True)
    finam_symbol = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    lot = models.PositiveIntegerField(default=1000)
    listed_at = models.DateTimeField(null=True, blank=True)
    roll_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "инструмент"
        verbose_name_plural = "инструменты"
        ordering = ["roll_at", "code"]

    def __str__(self):
        return self.code


class Candle(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="candles")
    ts = models.DateTimeField()
    open = models.DecimalField(max_digits=18, decimal_places=6)
    high = models.DecimalField(max_digits=18, decimal_places=6)
    low = models.DecimalField(max_digits=18, decimal_places=6)
    close = models.DecimalField(max_digits=18, decimal_places=6)
    volume = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    class Meta:
        verbose_name = "свеча"
        verbose_name_plural = "свечи"
        ordering = ["ts"]
        constraints = [
            models.UniqueConstraint(fields=["instrument", "ts"], name="uniq_candle_instrument_ts"),
        ]
        indexes = [models.Index(fields=["instrument", "ts"])]

    def __str__(self):
        return f"{self.instrument.code} {self.ts:%Y-%m-%d %H:%M}"