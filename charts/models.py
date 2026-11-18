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
