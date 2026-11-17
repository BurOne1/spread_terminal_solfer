from django.contrib import admin

from .models import Candle, Instrument, Pair


@admin.register(Pair)
class PairAdmin(admin.ModelAdmin):
    """Галочка «для просмотра» — какие пары видит группа «Просмотр»."""
    list_display = ("code", "title", "foreign_source", "active", "viewer")
    list_editable = ("active", "viewer")
    list_filter = ("active", "viewer", "foreign_source")
    search_fields = ("code", "title")


admin.site.register(Instrument)
admin.site.register(Candle)
