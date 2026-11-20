
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("api/spread/", views.spread_json, name="spread-json"),
    path("api/watchlist/", views.watchlist_json, name="watchlist-json"),
    path("api/published/", views.published_json, name="published-json"),
    path("api/alerts/", views.alerts_json, name="alerts-json"),
    path("api/lots/", views.lots_json, name="lots-json"),
    path("api/funding/", views.funding_json, name="funding-json"),
]
