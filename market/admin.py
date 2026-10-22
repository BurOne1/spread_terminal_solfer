from django.contrib import admin

from .models import Candle, Instrument

admin.site.register(Instrument)
admin.site.register(Candle)
