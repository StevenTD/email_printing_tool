from django.conf import settings
from django.db import models
from django.db.models import Q

from .storage import email_file_upload_to


class ReceivedEmail(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        QUEUED = "queued", "Queued"
        PRINTING = "printing", "Printing"
        PRINTED = "printed", "Printed"
        HELD = "held", "Held"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    message_id = models.CharField(max_length=998, blank=True, db_index=True)
    imap_folder = models.CharField(max_length=255, default="INBOX")
    imap_uidvalidity = models.PositiveBigIntegerField(default=0)
    imap_uid = models.PositiveBigIntegerField()
    sender = models.CharField(max_length=998, blank=True, db_index=True)
    recipients = models.JSONField(default=list, blank=True)
    to_recipients = models.JSONField(default=list, blank=True)
    cc_recipients = models.JSONField(default=list, blank=True)
    bcc_recipients = models.JSONField(default=list, blank=True)
    subject = models.TextField(blank=True)
    date_received = models.DateTimeField(null=True, blank=True, db_index=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    raw_email_file = models.FileField(upload_to=email_file_upload_to, blank=True)
    body_pdf_file = models.FileField(upload_to=email_file_upload_to, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    acknowledged = models.BooleanField(default=False, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-date_received", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["imap_folder", "imap_uidvalidity", "imap_uid"],
                name="unique_mailbox_uid",
            ),
            models.UniqueConstraint(
                fields=["imap_folder", "message_id"],
                condition=~Q(message_id=""),
                name="unique_mailbox_message_id",
            ),
        ]

    def __str__(self):
        return self.subject or self.message_id or f"Email UID {self.imap_uid}"


class EmailAttachment(models.Model):
    email = models.ForeignKey(
        ReceivedEmail,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    filename = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    file = models.FileField(upload_to=email_file_upload_to, blank=True)
    printable = models.BooleanField(default=False)
    conversion_error = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.filename


class Acknowledgement(models.Model):
    class Status(models.TextChoices):
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    class Source(models.TextChoices):
        MAILPRINT = "mailprint", "MailPrint"
        GMAIL = "gmail", "Gmail Sent"

    email = models.ForeignKey(
        ReceivedEmail,
        on_delete=models.CASCADE,
        related_name="acknowledgements",
    )
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.MAILPRINT,
    )
    recipient = models.CharField(max_length=998)
    subject = models.CharField(max_length=998, blank=True)
    body = models.TextField()
    template_name = models.CharField(max_length=150, blank=True)
    message_id = models.CharField(max_length=998, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-sent_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(status="sending"),
                name="one_acknowledgement_sending_per_email",
            )
        ]

    def __str__(self):
        return f"Acknowledgement for {self.email_id}"
