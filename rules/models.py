from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class FilterRule(models.Model):
    class SenderMatch(models.TextChoices):
        ANY = "any", "Any sender"
        EXACT = "exact", "Exact address"
        DOMAIN = "domain", "Domain"

    class SubjectMatch(models.TextChoices):
        ANY = "any", "Any subject"
        CONTAINS = "contains", "Contains"
        REGEX = "regex", "Regular expression"

    class Action(models.TextChoices):
        PRINT = "print", "Print"
        IGNORE = "ignore", "Ignore"
        HOLD = "hold", "Hold for review"

    name = models.CharField(max_length=150)
    enabled = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=100, db_index=True)
    sender_match = models.CharField(
        max_length=10,
        choices=SenderMatch.choices,
        default=SenderMatch.ANY,
    )
    sender_value = models.CharField(max_length=998, blank=True)
    subject_match = models.CharField(
        max_length=10,
        choices=SubjectMatch.choices,
        default=SubjectMatch.ANY,
    )
    subject_value = models.CharField(max_length=500, blank=True)
    attachment_type = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional MIME type or file extension, for example application/pdf or .pdf.",
    )
    action = models.CharField(max_length=10, choices=Action.choices, default=Action.HOLD)
    auto_print_override = models.BooleanField(
        null=True,
        blank=True,
        help_text=(
            "When enabled, True may bypass the schedule (but never the global OFF switch); "
            "False disables automatic printing for this rule."
        ),
    )
    target_printer = models.CharField(max_length=255, blank=True)
    copies = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(99)],
    )
    print_email_body = models.BooleanField(default=True)
    print_attachments = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return self.name


class AcknowledgementTemplate(models.Model):
    name = models.CharField(max_length=150, unique=True)
    body = models.TextField()
    enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="only_one_default_ack_template",
            )
        ]

    def __str__(self):
        return self.name
