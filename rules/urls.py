from django.urls import path

from . import views


app_name = "rules"

urlpatterns = [
    path("rules/", views.rule_list, name="list"),
    path("rules/new/", views.rule_create, name="create"),
    path("rules/<int:rule_id>/edit/", views.rule_edit, name="edit"),
    path("rules/<int:rule_id>/delete/", views.rule_delete, name="delete"),
    path("rules/schedule/", views.rule_schedule, name="schedule"),
    path("templates/", views.template_list, name="templates"),
    path("templates/new/", views.template_create, name="template_create"),
    path(
        "templates/<int:template_id>/edit/",
        views.template_edit,
        name="template_edit",
    ),
    path(
        "templates/<int:template_id>/delete/",
        views.template_delete,
        name="template_delete",
    ),
    path(
        "templates/auto-acknowledge/",
        views.toggle_auto_acknowledge,
        name="toggle_auto_acknowledge",
    ),
]
