
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("api/spread/", views.spread_json, name="spread-json"),
    path("api/watchlist/", views.watchlist_json, name="watchlist-json"),
]
