from django.contrib import admin

from .models import PublishedChannel


@admin.register(PublishedChannel)
class PublishedChannelAdmin(admin.ModelAdmin):
    list_display = ("pair", "drawing_id", "lines", "published_at", "zone", "event", "event_at")
    list_filter = ("pair",)
    readonly_fields = ("published_at",)
