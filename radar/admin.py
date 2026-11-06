from django.contrib import admin

from radar.models import Anomaly, SourceStatus, WatchItem


@admin.register(WatchItem)
class WatchItemAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "kind", "active", "large_pct", "wall_ratio", "note")
    list_editable = ("active", "large_pct", "wall_ratio")
    list_filter = ("kind", "active")
    search_fields = ("code", "title", "note")
    fieldsets = (
        (None, {"fields": ("code", "kind", "title", "active", "note")}),
        ("Пороги детекторов", {
            "fields": ("large_pct", "wall_ratio"),
            "description": "Ноги спредов берут значения по умолчанию. Изменения подхватываются "
                           "при следующем переоткрытии сокета run_radar, раз в 11 минут.",
        }),
    )


@admin.register(Anomaly)
class AnomalyAdmin(admin.ModelAdmin):
    list_display = ("ts", "symbol", "kind", "side", "price", "size", "score", "text")
    list_filter = ("kind", "symbol")
    date_hierarchy = "ts"
    search_fields = ("symbol", "text")


@admin.register(SourceStatus)
class SourceStatusAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "message", "updated_at")
    readonly_fields = ("name", "state", "message", "updated_at")

    def has_add_permission(self, request):
        return False
