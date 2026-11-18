from django.contrib import admin

from .models import AcknowledgementTemplate, FilterRule


@admin.register(FilterRule)
class FilterRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "priority", "action", "enabled", "target_printer")
    list_filter = ("enabled", "action", "sender_match", "subject_match")
    search_fields = ("name", "sender_value", "subject_value")
    ordering = ("priority", "id")


@admin.register(AcknowledgementTemplate)
class AcknowledgementTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "is_default", "updated_at")
    list_filter = ("enabled", "is_default")
    search_fields = ("name", "body")
