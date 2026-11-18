from django.contrib import admin

from .models import Acknowledgement, EmailAttachment, ReceivedEmail


class EmailAttachmentInline(admin.TabularInline):
    model = EmailAttachment
    extra = 0


@admin.register(ReceivedEmail)
class ReceivedEmailAdmin(admin.ModelAdmin):
    list_display = ("subject", "sender", "date_received", "status", "acknowledged")
    list_filter = ("status", "acknowledged", "date_received")
    search_fields = ("subject", "sender", "message_id")
    inlines = [EmailAttachmentInline]


@admin.register(Acknowledgement)
class AcknowledgementAdmin(admin.ModelAdmin):
    list_display = ("email", "recipient", "status", "sent_at", "sent_by")
    readonly_fields = (
        "email",
        "recipient",
        "subject",
        "body",
        "template_name",
        "message_id",
        "status",
        "sent_at",
        "sent_by",
        "error_message",
    )
