from django.contrib import admin

from .models import PrintJob, PrintLog, PrinterProfile


@admin.register(PrinterProfile)
class PrinterProfileAdmin(admin.ModelAdmin):
    list_display = ("printer_name", "alias", "is_default", "dedicated_purpose")
    list_filter = ("is_default", "color_mode", "duplex")
    search_fields = ("printer_name", "alias", "dedicated_purpose")


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "email",
        "printer_name",
        "status",
        "automatic",
        "created_at",
        "requested_by",
    )
    list_filter = ("status", "automatic", "dry_run", "explicit_reprint")
    readonly_fields = ("created_at", "started_at", "completed_at")


@admin.register(PrintLog)
class PrintLogAdmin(admin.ModelAdmin):
    list_display = ("printed_at", "email", "filename", "printer_name", "result", "user")
    list_filter = ("result", "printed_at")
    search_fields = ("filename", "printer_name", "details")
    readonly_fields = (
        "email",
        "job",
        "filename",
        "printer_name",
        "result",
        "details",
        "printed_at",
        "user",
    )
