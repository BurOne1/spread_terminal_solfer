from django.urls import path

from . import views

urlpatterns = [
    path("radar/", views.page, name="radar"),
    path("api/radar/state/", views.state_json, name="radar-state"),
    path("api/radar/chart/", views.chart_json, name="radar-chart"),
    path("api/radar/instruments/", views.instruments_json, name="radar-instruments"),
]
