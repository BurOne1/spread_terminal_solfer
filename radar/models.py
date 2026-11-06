"""Радар: следы крупных участников в стакане и ленте.

Пишет процесс run_radar, читает страница /radar/. Сырой стакан в базу не
попадает: в BookState одна строка на символ с последними верхними уровнями.
Сделки хранятся несколько дней, минутные агрегаты и аномалии — долго.
"""

from django.db import models


class SourceStatus(models.Model):
    """Состояние источника данных. radar — отметка жизни самого run_radar."""

    class State(models.TextChoices):
        OK = "ok", "подключён"
        WAIT = "wait", "подключается"
        NO_KEY = "no_key", "нет ключа"
        ERROR = "error", "ошибка"

    name = models.CharField(max_length=16, unique=True)
    state = models.CharField(max_length=8, choices=State.choices, default=State.WAIT)
    message = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField()

    class Meta:
        verbose_name = "источник"
        verbose_name_plural = "источники"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}: {self.get_state_display()}"


class WatchItem(models.Model):
    """Инструмент сверх ног спредов: ноги радар подписывает сам и сам переводит после склейки."""

    class Kind(models.TextChoices):
        QUARTER = "quarter", "квартальный фьючерс, корень"
        SYMBOL = "symbol", "символ Finam"

    title = models.CharField("название", max_length=64, blank=True)
    kind = models.CharField("тип", max_length=8, choices=Kind.choices, default=Kind.QUARTER)
    code = models.CharField(
        "код", max_length=32,
        help_text="Корень квартала Мосбиржи (RI, MX) — фронт выбирается сам. Или символ Finam: IMOEXF@RTSX.",
    )
    active = models.BooleanField("следить", default=True)
    large_pct = models.FloatField(
        "процентиль крупной сделки", default=99.5,
        help_text="Сделка крупнее этого процентиля размеров инструмента — крупный принт.",
    )
    wall_ratio = models.FloatField(
        "стена, раз к медиане уровня", default=5.0,
        help_text="Стена: уровень во столько раз больше медианного и выше 99,5-го процентиля уровней инструмента.",
    )
    note = models.CharField("заметка", max_length=255, blank=True)

    class Meta:
        verbose_name = "инструмент радара"
        verbose_name_plural = "инструменты радара"
        ordering = ["code"]

    def __str__(self):
        return self.title or self.code


class Trade(models.Model):
    """Сделка из ленты. side: 1 — агрессивная покупка, −1 — продажа, 0 — неизвестно."""

    symbol = models.CharField(max_length=32)
    trade_id = models.CharField(max_length=40)
    ts = models.DateTimeField()
    price = models.DecimalField(max_digits=18, decimal_places=6)
    size = models.DecimalField(max_digits=18, decimal_places=2)
    side = models.SmallIntegerField(default=0)
    oi = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "сделка"
        verbose_name_plural = "сделки"
        constraints = [models.UniqueConstraint(fields=["symbol", "trade_id"], name="uniq_radar_trade")]
        indexes = [models.Index(fields=["symbol", "ts"]), models.Index(fields=["ts"])]


class MinuteStat(models.Model):
    """Минута ленты: свеча, покупки и продажи агрессором, крупные сделки, открытый интерес."""

    symbol = models.CharField(max_length=32)
    ts = models.DateTimeField()
    open = models.DecimalField(max_digits=18, decimal_places=6)
    high = models.DecimalField(max_digits=18, decimal_places=6)
    low = models.DecimalField(max_digits=18, decimal_places=6)
    close = models.DecimalField(max_digits=18, decimal_places=6)
    volume = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    buy = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    sell = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    trades = models.PositiveIntegerField(default=0)
    large = models.PositiveIntegerField(default=0)
    oi = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "минута"
        verbose_name_plural = "минуты"
        constraints = [models.UniqueConstraint(fields=["symbol", "ts"], name="uniq_radar_minute")]
        indexes = [models.Index(fields=["symbol", "ts"])]


class BookState(models.Model):
    """Последний стакан символа и сводка для списка. Строка есть у каждого символа радара."""

    symbol = models.CharField(max_length=32, unique=True)
    title = models.CharField(max_length=64, blank=True)
    group = models.CharField(max_length=16, blank=True)
    ts = models.DateTimeField(null=True, blank=True)
    # [[цена, объём, стена], ...]: продажи от лучшей вверх, покупки от лучшей вниз.
    asks = models.JSONField(default=list)
    bids = models.JSONField(default=list)
    summary = models.JSONField(default=dict)
    updated_at = models.DateTimeField()

    class Meta:
        verbose_name = "стакан"
        verbose_name_plural = "стаканы"
        ordering = ["group", "symbol"]


class Anomaly(models.Model):
    class Kind(models.TextChoices):
        LARGE = "large", "крупная сделка"
        SWEEP = "sweep", "проход по стакану"
        WALL = "wall", "стена"
        PULLED = "pulled", "стена снята"
        EATEN = "eaten", "стена съедена"
        VOLUME = "volume", "всплеск объёма"
        OI = "oi", "скачок ОИ"

    symbol = models.CharField(max_length=32)
    ts = models.DateTimeField()
    kind = models.CharField(max_length=8, choices=Kind.choices)
    side = models.SmallIntegerField(default=0)
    price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    size = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    score = models.FloatField(default=0)
    text = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "аномалия"
        verbose_name_plural = "аномалии"
        ordering = ["-ts"]
        indexes = [models.Index(fields=["ts"]), models.Index(fields=["symbol", "ts"])]

    def __str__(self):
        return f"{self.ts:%H:%M:%S} {self.symbol} {self.get_kind_display()}"
