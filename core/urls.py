from django.urls import path

from . import views


app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("partials/dashboard-stats/", views.dashboard_stats, name="dashboard_stats"),
    path("auto-print/", views.toggle_auto_print, name="toggle_auto_print"),
]
