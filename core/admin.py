from django.contrib import admin

from .models import AppConfiguration, AuditLog, WorkerHeartbeat, WorkerLog


@admin.register(AppConfiguration)
class AppConfigurationAdmin(admin.ModelAdmin):
    readonly_fields = ("imap_app_password_encrypted", "smtp_app_password_encrypted", "updated_at")


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(admin.ModelAdmin):
    readonly_fields = ("process_id", "started_at", "last_seen_at", "last_fetch_at", "last_error")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "object_type", "object_id")
    list_filter = ("action", "object_type")
    search_fields = ("action", "object_type", "object_id", "user__username")
    readonly_fields = ("user", "action", "object_type", "object_id", "details", "created_at")


@admin.register(WorkerLog)
class WorkerLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "level", "component", "email")
    list_filter = ("level", "component", "created_at")
    search_fields = ("message", "email__subject", "email__sender")
    readonly_fields = ("level", "component", "message", "email", "created_at")
