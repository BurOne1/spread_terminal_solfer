from django.contrib import admin, messages
from django.db.models import Case, IntegerField, Value, When
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from radar.models import Anomaly, SignalRule, SourceStatus, WatchItem
from radar.rules import DEFAULTS, FIELDS


@admin.register(WatchItem)
class WatchItemAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "kind", "active", "note")
    list_editable = ("active",)
    list_filter = ("kind", "active")
    search_fields = ("code", "title", "note")
    fieldsets = (
        (None, {
            "fields": ("code", "kind", "title", "active", "note"),
            "description": "С галочкой «следить» — инструмент сверх ног спредов. Без галочки — "
                           "радар не слушает этот символ или корень, даже если это нога спреда. "
                           "Изменения подхватываются при переоткрытии сокета run_radar, раз в 11 минут. "
                           "Пороги сигналов — в разделе «Правила сигналов».",
        }),
    )


@admin.register(SignalRule)
class SignalRuleAdmin(admin.ModelAdmin):
    """Свой набор полей у каждого сигнала: подписи и подсказки из radar.rules.FIELDS."""

    list_display = ("kind_label", "scope_label", "enabled", "params", "note")
    list_display_links = ("kind_label",)
    list_editable = ("enabled",)
    list_filter = ("kind", "enabled")
    search_fields = ("scope", "note")

    def get_ordering(self, request):
        # Сигналы в порядке описания: крупная сделка, проход, стены, объём, ОИ; внутри — общее правило первым.
        order = Case(*[When(kind=kind, then=Value(index)) for index, kind in enumerate(DEFAULTS)], output_field=IntegerField())
        return (order.asc(), "scope")

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("kind", "scope", "enabled", "note")
        return ("kind", "scope", "enabled", *FIELDS[obj.kind], "note")

    def get_readonly_fields(self, request, obj=None):
        # Вид задаёт набор полей: у сохранённого правила его не меняют.
        return ("kind",) if obj is not None else ()

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is not None:
            for name, (label, hint) in FIELDS[obj.kind].items():
                if name in form.base_fields:
                    tail = "как в общем правиле" if obj.scope else f"{DEFAULTS[obj.kind][name]:g}"
                    form.base_fields[name].label = label
                    form.base_fields[name].help_text = f"{hint} Пусто — {tail}."
        return form

    def response_add(self, request, obj, post_url_continue=None):
        # Поля параметров зависят от вида сигнала, поэтому задаются после выбора вида.
        self.message_user(request, "Правило создано. Задайте параметры: пустые берутся из общего правила.", messages.INFO)
        return HttpResponseRedirect(reverse("admin:radar_signalrule_change", args=[obj.pk]))

    @admin.display(description="сигнал", ordering="kind")
    def kind_label(self, obj):
        return obj.get_kind_display()

    @admin.display(description="для кого", ordering="scope")
    def scope_label(self, obj):
        return obj.scope or format_html("<b>{}</b>", "все инструменты")

    @admin.display(description="параметры")
    def params(self, obj):
        parts = []
        for name, (label, _hint) in FIELDS[obj.kind].items():
            value = getattr(obj, name)
            if value is None and obj.scope:
                continue
            shown = DEFAULTS[obj.kind][name] if value is None else value
            parts.append(f"{label.lower()}: {shown:g}" if isinstance(shown, float) else f"{label.lower()}: {shown}")
        return "; ".join(parts) or "как в общем правиле"

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "title": "Правила сигналов: изменения радар подхватывает за 30 секунд"}
        return super().changelist_view(request, extra_context)


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
