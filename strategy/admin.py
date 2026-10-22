from django.contrib import admin

from .models import PaperTrade, PositionState, SpreadBar

admin.site.register(SpreadBar)
admin.site.register(PositionState)
admin.site.register(PaperTrade)
