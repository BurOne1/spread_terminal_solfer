from django.db import models


class PublishedChannel(models.Model):
    """Канал, опубликованный аналитиком для зрителей: точная копия рисунка.

    data — JSON рисунка библиотеки (точки время/цена, стиль), как в браузере
    аналитика. Повторная публикация того же рисунка (drawing_id) обновляет запись.
    lines — на какой линии рисовали: rate (очищенный) или raw (сырой).
    zone, event, event_at — состояние оповещений в Телеграм (charts/alerts.py).
    """

    pair = models.ForeignKey("market.Pair", on_delete=models.CASCADE, related_name="published_channels")
    drawing_id = models.CharField(max_length=64)
    data = models.JSONField()
    lines = models.CharField(max_length=8, default="rate")
    # Таймфрейм, на котором нарисован канал: прямой он по номерам баров именно этого таймфрейма.
    frame = models.CharField(max_length=4, default="H1")
    published_at = models.DateTimeField(auto_now=True)
    zone = models.CharField(max_length=8, blank=True)
    event = models.CharField(max_length=8, blank=True)
    event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "опубликованный канал"
        verbose_name_plural = "опубликованные каналы"
        ordering = ["pair", "published_at"]
        constraints = [models.UniqueConstraint(fields=["pair", "drawing_id"], name="uniq_published_channel")]

    def __str__(self):
        return f"{self.pair_id} канал {self.drawing_id}"


class Alert(models.Model):
    """Личный алерт аналитика по рисунку: одно срабатывание, в «Просмотр» не попадает.

    data — JSON рисунка библиотеки, как у PublishedChannel. kind — его вид:
    горизонтальная линия, трендовая линия или луч, параллельный канал, прямоугольник.
    lines — по какому ряду проверять: rate (очищенный) или raw (сырой).
    side — где был спред на прошлой проверке (above, below, on, in, upper, lower; пусто —
    ещё не проверялся или вне времени рисунка). Срабатывает на смене стороны, один раз:
    потом status = fired, проверка до повторного включения не идёт. См. charts/user_alerts.py.
    """

    ACTIVE, FIRED, PAUSED = "active", "fired", "paused"
    STATUSES = [(ACTIVE, "ждёт"), (FIRED, "сработал"), (PAUSED, "выключен")]
    # Канал: на что срабатывать — касание границ, переход середины или и то, и другое.
    EDGES, MIDDLE, BOTH = "edges", "middle", "both"
    TARGETS = [(EDGES, "границы"), (MIDDLE, "середина"), (BOTH, "границы и середина")]

    pair = models.ForeignKey("market.Pair", on_delete=models.CASCADE, related_name="alerts")
    drawing_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=24)
    data = models.JSONField()
    lines = models.CharField(max_length=8, default="rate")
    frame = models.CharField(max_length=4, default="H1")
    status = models.CharField(max_length=8, choices=STATUSES, default=ACTIVE)
    target = models.CharField(max_length=8, choices=TARGETS, default=BOTH)
    # Повторяющийся алерт (канал, трендовая линия, луч): после срабатывания не выключается,
    # следующее сообщение — не раньше чем через pause_minutes. fired_* — последнее срабатывание.
    repeat = models.BooleanField(default=False)
    pause_minutes = models.PositiveIntegerField(default=15)
    # Склейка, перед которой алерт сняли автоматически (charts/user_alerts.ROLL_OFF): включили
    # снова руками — перед этой же склейкой второй раз не снимается.
    roll_off_at = models.DateTimeField(null=True, blank=True)
    side = models.CharField(max_length=8, blank=True)
    last_value = models.FloatField(null=True, blank=True)
    fired_at = models.DateTimeField(null=True, blank=True)
    fired_value = models.FloatField(null=True, blank=True)
    fired_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "алерт"
        verbose_name_plural = "алерты"
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["pair", "drawing_id"], name="uniq_alert_drawing")]

    def __str__(self):
        return f"{self.pair_id} алерт {self.kind} {self.drawing_id}"
