from django.urls import path

from . import views

urlpatterns = [
    path("analytics/", views.page, name="analytics"),
    path("api/analytics/", views.analytics_json, name="analytics-json"),
    path("api/analytics/contracts/", views.contracts_json, name="analytics-contracts"),
    path("api/analytics/export.xlsx", views.analytics_xlsx, name="analytics-xlsx"),
]
