from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class PrinterProfile(models.Model):
    class ColorMode(models.TextChoices):
        COLOR = "color", "Color"
        MONO = "mono", "Monochrome"
        PRINTER_DEFAULT = "default", "Printer default"

    printer_name = models.CharField(max_length=255, unique=True)
    alias = models.CharField(max_length=150, blank=True)
    is_default = models.BooleanField(default=False)
    dedicated_purpose = models.CharField(max_length=150, blank=True)
    paper_size = models.CharField(max_length=30, default="A4")
    duplex = models.BooleanField(default=False)
    color_mode = models.CharField(
        max_length=10,
        choices=ColorMode.choices,
        default=ColorMode.PRINTER_DEFAULT,
    )

    class Meta:
        ordering = ["alias", "printer_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="only_one_default_printer",
            )
        ]

    def __str__(self):
        return self.alias or self.printer_name


class PrintJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PRINTING = "printing", "Printing"
        PRINTED = "printed", "Printed"
        DRY_RUN = "dry_run", "Dry run"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    email = models.ForeignKey(
        "mails.ReceivedEmail",
        on_delete=models.CASCADE,
        related_name="print_jobs",
    )
    prior_email_status = models.CharField(max_length=10, blank=True)
    printer_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    copies = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(99)],
    )
    print_email_body = models.BooleanField(default=True)
    print_replies = models.BooleanField(default=False)
    print_attachments = models.BooleanField(default=True)
    explicit_reprint = models.BooleanField(default=False)
    automatic = models.BooleanField(default=False, db_index=True)
    dry_run = models.BooleanField(default=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Print job {self.pk} for email {self.email_id}"


class PrintLog(models.Model):
    class Result(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILURE = "failure", "Failure"
        DRY_RUN = "dry_run", "Dry run"
        MANUAL = "manual", "Marked as printed"
        SKIPPED = "skipped", "Skipped"

    email = models.ForeignKey(
        "mails.ReceivedEmail",
        on_delete=models.CASCADE,
        related_name="print_logs",
    )
    job = models.ForeignKey(
        PrintJob,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="logs",
    )
    filename = models.CharField(max_length=500, blank=True)
    printer_name = models.CharField(max_length=255, blank=True)
    result = models.CharField(max_length=10, choices=Result.choices)
    details = models.TextField(blank=True)
    printed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["-printed_at"]

    def __str__(self):
        return f"{self.get_result_display()} for email {self.email_id}"
