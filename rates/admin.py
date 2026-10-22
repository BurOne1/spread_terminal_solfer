from django.contrib import admin

from .models import FundingDay, RateDay

admin.site.register(RateDay)
admin.site.register(FundingDay)
