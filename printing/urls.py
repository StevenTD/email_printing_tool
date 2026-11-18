from django.urls import path

from . import views


app_name = "printing"

urlpatterns = [
    path("printers/", views.printer_list, name="printers"),
    path("printers/settings/sumatra/", views.sumatra_settings, name="sumatra_settings"),
    path("printers/profiles/new/", views.printer_profile_create, name="profile_create"),
    path(
        "printers/profiles/<int:profile_id>/edit/",
        views.printer_profile_edit,
        name="profile_edit",
    ),
    path("printers/test-page/", views.printer_test_page, name="test_page"),
    path("printers/jobs/cancel/", views.printer_job_cancel, name="job_cancel"),
]
