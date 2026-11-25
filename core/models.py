from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class AppConfiguration(models.Model):
    class DefaultAction(models.TextChoices):
        PRINT = "print", "Print"
        HOLD = "hold", "Hold for review"
        IGNORE = "ignore", "Ignore"

    key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    auto_print_enabled = models.BooleanField(default=False)
    poll_interval_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(15)],
    )
    imap_folder = models.CharField(max_length=255, default="INBOX")
    imap_sent_folder = models.CharField(
        max_length=255,
        default="[Gmail]/Sent Mail",
        blank=True,
    )
    imap_sent_sync_folder = models.CharField(
        max_length=255,
        blank=True,
        editable=False,
    )
    imap_sent_uidvalidity = models.PositiveBigIntegerField(
        default=0,
        editable=False,
    )
    imap_sent_last_uid = models.PositiveBigIntegerField(
        default=0,
        editable=False,
    )
    default_action = models.CharField(
        max_length=10,
        choices=DefaultAction.choices,
        default=DefaultAction.HOLD,
    )
    dry_run_enabled = models.BooleanField(default=False)
    auto_acknowledge_after_print = models.BooleanField(default=False)
    office_name = models.CharField(max_length=150, blank=True)
    sumatra_pdf_path = models.CharField(max_length=500, blank=True)
    imap_email = models.EmailField(blank=True)
    imap_app_password_encrypted = models.TextField(blank=True, editable=False)
    smtp_app_password_encrypted = models.TextField(blank=True, editable=False)
    schedule_enabled = models.BooleanField(default=False)
    schedule_weekdays = models.JSONField(default=list)
    schedule_start = models.TimeField(null=True, blank=True)
    schedule_end = models.TimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "application configuration"
        verbose_name_plural = "application configuration"

    def save(self, *args, **kwargs):
        self.key = 1
        super().save(*args, **kwargs)

    def set_imap_app_password(self, value):
        from .secrets import encrypt_secret

        self.imap_app_password_encrypted = encrypt_secret(value)

    def get_imap_app_password(self):
        from .secrets import decrypt_secret

        return decrypt_secret(self.imap_app_password_encrypted)

    def set_smtp_app_password(self, value):
        from .secrets import encrypt_secret

        self.smtp_app_password_encrypted = encrypt_secret(value)

    def get_smtp_app_password(self):
        from .secrets import decrypt_secret

        return decrypt_secret(self.smtp_app_password_encrypted)

    def __str__(self):
        return "MailPrint configuration"


class WorkerHeartbeat(models.Model):
    key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    running = models.BooleanField(default=False)
    process_id = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_fetch_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        self.key = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "MailPrint worker status"


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mailprint_audit_events",
    )
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} at {self.created_at:%Y-%m-%d %H:%M}"


class WorkerLog(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    level = models.CharField(max_length=10, choices=Level.choices, db_index=True)
    component = models.CharField(max_length=100, default="worker")
    message = models.TextField()
    email = models.ForeignKey(
        "mails.ReceivedEmail",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="worker_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_level_display()}: {self.message[:80]}"


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mailprint_profile",
    )
    department = models.CharField(max_length=150, blank=True)

    def __str__(self):
        return self.department or self.user.get_username()


def get_user_department(user):
    profile = getattr(user, "mailprint_profile", None) if user is not None else None
    return profile.department.strip() if profile else ""
