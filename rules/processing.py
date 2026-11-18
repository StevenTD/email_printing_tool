from django.utils import timezone

from core.models import AppConfiguration, WorkerLog
from mails.models import ReceivedEmail
from printing.pipeline import create_print_job

from .matching import find_matching_rule
from .models import FilterRule


def is_within_auto_print_schedule(configuration, *, now=None):
    if not configuration.schedule_enabled:
        return True
    if not configuration.schedule_start or not configuration.schedule_end:
        return False
    local_now = timezone.localtime(now or timezone.now())
    if local_now.weekday() not in configuration.schedule_weekdays:
        return False
    return configuration.schedule_start <= local_now.time() <= configuration.schedule_end


def process_incoming_email(email, *, now=None):
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    rule = find_matching_rule(email)
    action = rule.action if rule else configuration.default_action

    if action == FilterRule.Action.IGNORE:
        _set_status(email, ReceivedEmail.Status.SKIPPED)
        _log(email, WorkerLog.Level.INFO, "Email skipped by filtering rules.")
        return None
    if action == FilterRule.Action.HOLD:
        _set_status(email, ReceivedEmail.Status.HELD)
        _log(email, WorkerLog.Level.INFO, "Email held for review.")
        return None

    printable_attachment_rule = bool(
        rule
        and action == FilterRule.Action.PRINT
        and rule.print_attachments
        and email.attachments.filter(printable=True).exists()
    )
    if not configuration.auto_print_enabled and not printable_attachment_rule:
        _set_status(email, ReceivedEmail.Status.HELD)
        _log(email, WorkerLog.Level.INFO, "Auto-print is off; email held for review.")
        return None

    bypass_schedule = bool(rule and rule.auto_print_override is True)
    if rule and rule.auto_print_override is False:
        _set_status(email, ReceivedEmail.Status.HELD)
        _log(email, WorkerLog.Level.INFO, "Auto-print disabled by the matching rule.")
        return None
    if not bypass_schedule and not is_within_auto_print_schedule(configuration, now=now):
        _set_status(email, ReceivedEmail.Status.HELD)
        _log(email, WorkerLog.Level.INFO, "Email held outside the auto-print schedule.")
        return None

    job = create_print_job(
        email,
        printer_name=rule.target_printer if rule else "",
        copies=rule.copies if rule else 1,
        print_email_body=rule.print_email_body if rule else True,
        print_attachments=rule.print_attachments if rule else True,
        automatic=True,
    )
    queue_reason = (
        "Queued by a Print rule with printable attachments while global auto-print is off."
        if printable_attachment_rule and not configuration.auto_print_enabled
        else f"Queued automatically as print job {job.pk}."
    )
    _log(email, WorkerLog.Level.INFO, queue_reason)
    return job


def _set_status(email, status):
    email.status = status
    email.save(update_fields=["status"])


def _log(email, level, message):
    WorkerLog.objects.create(
        level=level,
        component="rules",
        message=message,
        email=email,
    )
