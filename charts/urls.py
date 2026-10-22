
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("api/spread/", views.spread_json, name="spread-json"),
    path("api/levels/", views.save_levels, name="save-levels"),
    path("api/recalc/", views.recalc, name="recalc"),
]