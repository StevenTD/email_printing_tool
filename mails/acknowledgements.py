import html
import re
import smtplib
import ssl
from email import message_from_bytes
from email.message import EmailMessage as MimeEmailMessage
from email.policy import default
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from string import Formatter

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import AppConfiguration, AuditLog, WorkerLog
from rules.models import AcknowledgementTemplate

from .models import Acknowledgement, ReceivedEmail


ACKNOWLEDGEMENT_VARIABLES = {
    "sender_name",
    "subject",
    "date",
    "office_name",
    "tracking_number",
    "tracking_base_url",
    "tracking_url",
    "edats_sender",
}
MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")


class AcknowledgementError(Exception):
    pass


class DuplicateAcknowledgementError(AcknowledgementError):
    pass


class AcknowledgementSendError(AcknowledgementError):
    pass


def _acknowledgement_variables(email, configuration, extra_variables=None):
    display_name, address = parseaddr(email.sender)
    if not address:
        raise AcknowledgementError("The sender address is missing or invalid.")
    date = (
        timezone.localtime(email.date_received).strftime("%Y-%m-%d %H:%M %Z")
        if email.date_received
        else ""
    )
    variables = {
        "sender_name": display_name or address.split("@", 1)[0],
        "subject": email.subject or "(no subject)",
        "date": date,
        "office_name": configuration.office_name,
        "tracking_number": "",
        "tracking_base_url": "",
        "tracking_url": "",
        "edats_sender": "",
    }
    if extra_variables:
        variables.update(extra_variables)
    return variables


def render_acknowledgement_body(email, template, configuration, extra_variables=None):
    variables = _acknowledgement_variables(
        email,
        configuration,
        extra_variables=extra_variables,
    )
    validate_acknowledgement_template(template.body)
    formatter = Formatter()
    return formatter.vformat(template.body, (), variables)


def render_acknowledgement_html(template_body, variables):
    validate_acknowledgement_html_template(template_body)
    safe_variables = {
        key: html.escape(str(value), quote=True)
        for key, value in variables.items()
    }
    return re.sub(
        r"\{([A-Za-z_][A-Za-z_0-9]*)\}",
        lambda match: safe_variables[match.group(1)],
        template_body,
    )


def validate_acknowledgement_template(body):
    formatter = Formatter()
    try:
        parsed_fields = list(formatter.parse(body))
        for _, field_name, format_spec, conversion in parsed_fields:
            if field_name is None:
                continue
            if field_name not in ACKNOWLEDGEMENT_VARIABLES:
                raise AcknowledgementError(
                    f"Unsupported acknowledgement template variable: {field_name}"
                )
            if format_spec or conversion:
                raise AcknowledgementError(
                    "Acknowledgement template variables cannot use format modifiers."
                )
    except ValueError as exc:
        raise AcknowledgementError(f"Invalid acknowledgement template: {exc}") from exc


def validate_acknowledgement_html_template(body):
    for field_name in re.findall(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", body):
        if field_name not in ACKNOWLEDGEMENT_VARIABLES:
            raise AcknowledgementError(
                f"Unsupported acknowledgement template variable: {field_name}"
            )


def build_reply_message(
    email,
    template,
    configuration,
    *,
    extra_variables=None,
    html_body_template="",
):
    _, recipient = parseaddr(email.sender)
    if not recipient or "\r" in recipient or "\n" in recipient:
        raise AcknowledgementError("The sender address is missing or invalid.")
    parent_message_id = (email.message_id or "").strip()
    if not MESSAGE_ID_PATTERN.fullmatch(parent_message_id):
        raise AcknowledgementError("The incoming email has no valid Message-ID to reply to.")

    variables = _acknowledgement_variables(
        email,
        configuration,
        extra_variables=extra_variables,
    )
    validate_acknowledgement_template(template.body)
    body = Formatter().vformat(template.body, (), variables)
    subject = email.subject or "(no subject)"
    reply_subject = subject if subject.casefold().startswith("re:") else f"Re: {subject}"
    references = _thread_references(email, parent_message_id)
    message = MimeEmailMessage()
    message["From"] = formataddr(
        (configuration.office_name, configuration.imap_email),
        charset="utf-8",
    )
    message["To"] = recipient
    message["Subject"] = reply_subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="mailprint.local")
    message["In-Reply-To"] = parent_message_id
    message["References"] = " ".join(references)
    message.set_content(body, charset="utf-8")
    if html_body_template:
        message.add_alternative(
            render_acknowledgement_html(html_body_template, variables),
            subtype="html",
            charset="utf-8",
        )
    return message, recipient, body


def _thread_references(email, parent_message_id):
    references = []
    if email.raw_email_file:
        try:
            with email.raw_email_file.open("rb") as source:
                original_message = message_from_bytes(source.read(), policy=default)
            references = [
                candidate
                for candidate in str(original_message.get("References", "")).split()
                if MESSAGE_ID_PATTERN.fullmatch(candidate)
            ]
        except OSError:
            references = []
    if parent_message_id not in references:
        references.append(parent_message_id)
    references = list(dict.fromkeys(references))
    while len(references) > 1 and len(" ".join(references)) > 900:
        references.pop(0)
    return references


def send_acknowledgement(
    email,
    template,
    *,
    user=None,
    confirm_duplicate=False,
    extra_variables=None,
    html_body_template="",
):
    if not template.enabled:
        raise AcknowledgementError("This acknowledgement template is disabled.")
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    message, recipient, body = build_reply_message(
        email,
        template,
        configuration,
        extra_variables=extra_variables,
        html_body_template=html_body_template,
    )
    subject = str(message["Subject"])
    try:
        with transaction.atomic():
            locked_email = ReceivedEmail.objects.select_for_update().get(pk=email.pk)
            if (
                not confirm_duplicate
                and (
                    locked_email.acknowledged
                    or locked_email.acknowledgements.filter(
                        status=Acknowledgement.Status.SENT
                    ).exists()
                )
            ):
                raise DuplicateAcknowledgementError(
                    "An acknowledgement was already sent; confirm to send another."
                )
            if locked_email.acknowledgements.filter(
                status=Acknowledgement.Status.SENDING
            ).exists():
                raise DuplicateAcknowledgementError(
                    "An acknowledgement send is already in progress."
                )
            acknowledgement = Acknowledgement.objects.create(
                email=locked_email,
                recipient=recipient,
                sender=configuration.imap_email,
                subject=subject,
                body=body,
                body_html=(
                    message.get_body(preferencelist=("html",)).get_content()
                    if html_body_template
                    else ""
                ),
                template_name=template.name,
                message_id=str(message["Message-ID"]),
                status=Acknowledgement.Status.SENDING,
                sent_by=user,
            )
    except IntegrityError as exc:
        if Acknowledgement.objects.filter(
            email=email,
            status=Acknowledgement.Status.SENDING,
        ).exists():
            raise DuplicateAcknowledgementError(
                "An acknowledgement send is already in progress."
            ) from exc
        raise

    try:
        app_password = configuration.get_smtp_app_password()
        if not configuration.imap_email or not app_password:
            raise ImproperlyConfigured(
                "Run 'python manage.py configure_mail' to configure Gmail SMTP credentials."
            )
        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            context=ssl.create_default_context(),
            timeout=30,
        ) as smtp:
            smtp.login(configuration.imap_email, app_password)
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    except (
        ImproperlyConfigured,
        OSError,
        smtplib.SMTPException,
        UnicodeError,
        ValueError,
    ) as exc:
        acknowledgement.status = Acknowledgement.Status.FAILED
        acknowledgement.error_message = str(exc)
        acknowledgement.save(update_fields=["status", "error_message"])
        WorkerLog.objects.create(
            level=WorkerLog.Level.ERROR,
            component="smtp",
            message=f"Acknowledgement failed for email {email.pk}: {exc}",
            email=email,
        )
        raise AcknowledgementSendError(str(exc)) from exc

    sent_at = timezone.now()
    acknowledgement.status = Acknowledgement.Status.SENT
    acknowledgement.sent_at = sent_at
    acknowledgement.error_message = ""
    acknowledgement.save(update_fields=["status", "sent_at", "error_message"])
    email.acknowledged = True
    email.acknowledged_at = sent_at
    email.save(update_fields=["acknowledged", "acknowledged_at"])
    AuditLog.objects.create(
        user=user,
        action="acknowledgement_sent",
        object_type="ReceivedEmail",
        object_id=str(email.pk),
        details={"recipient": recipient, "template": template.name},
    )
    return acknowledgement


def send_auto_acknowledgement(email):
    if email.acknowledged:
        return None
    template = AcknowledgementTemplate.objects.filter(
        enabled=True,
        is_default=True,
    ).first()
    if template is None:
        WorkerLog.objects.create(
            level=WorkerLog.Level.ERROR,
            component="smtp",
            message="Auto-acknowledgement is enabled but no default template is configured.",
            email=email,
        )
        return None
    try:
        return send_acknowledgement(email, template)
    except AcknowledgementError:
        return None
