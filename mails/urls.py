from django.urls import path

from . import views


app_name = "mails"

urlpatterns = [
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/rows/", views.inbox_rows, name="inbox_rows"),
    path("inbox/bulk-action/", views.bulk_action, name="bulk_action"),
    path("emails/<int:email_id>/", views.email_detail, name="detail"),
    path(
        "emails/<int:email_id>/client-print/",
        views.client_print_email,
        name="client_print_email",
    ),
    path(
        "emails/<int:email_id>/acknowledgement/",
        views.acknowledgement_compose,
        name="acknowledgement",
    ),
    path("emails/<int:email_id>/actions/<str:action>/", views.email_action, name="action"),
    path(
        "attachments/<int:attachment_id>/download/",
        views.download_attachment,
        name="download_attachment",
    ),
    path(
        "attachments/<int:attachment_id>/client-print/",
        views.client_print_attachment,
        name="client_print_attachment",
    ),
    path(
        "attachments/<int:attachment_id>/preview/",
        views.preview_attachment,
        name="preview_attachment",
    ),
    path(
        "emails/<int:email_id>/download/",
        views.download_raw_email,
        name="download_raw_email",
    ),
]
