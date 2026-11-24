import hashlib
import shutil
import tempfile
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core import signing
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import is_mailprint_admin, require_mailprint_operator
from core.models import AppConfiguration, AuditLog
from printing.conversion import (
    ConversionError,
    convert_attachment_to_pdf,
    render_email_to_pdf,
)
from printing.models import PrintJob, PrintLog
from printing.pipeline import PrintPipelineError, create_print_job

from .acknowledgements import (
    AcknowledgementError,
    DuplicateAcknowledgementError,
    build_reply_message,
    send_acknowledgement,
)
from .edats import (
    EdatsLookupError,
    build_public_tracking_url,
    fetch_edats_document,
    subjects_match,
)
from .forms import AcknowledgementSendForm, EdatsAcknowledgementSendForm
from .models import Acknowledgement, EmailAttachment, ReceivedEmail
from .rendering import build_email_display_document, sanitize_email_html


PAGE_SIZE = 25
ACKNOWLEDGEMENT_PREVIEW_SALT = "mails.acknowledgement-preview"
EDATS_ACKNOWLEDGEMENT_PREVIEW_SALT = "mails.edats-acknowledgement-preview"


def _filtered_emails(request):
    emails = ReceivedEmail.objects.prefetch_related("attachments").annotate(
        has_reply=Exists(
            Acknowledgement.objects.filter(
                email_id=OuterRef("pk"),
                status=Acknowledgement.Status.SENT,
            )
        )
    )
    status = request.GET.get("status", "")
    valid_statuses = {value for value, _ in ReceivedEmail.Status.choices}
    if status in valid_statuses:
        emails = emails.filter(status=status)

    search = request.GET.get("q", "").strip()
    if search:
        emails = emails.filter(
            Q(sender__icontains=search)
            | Q(subject__icontains=search)
            | Q(message_id__icontains=search)
        )

    has_attachment = request.GET.get("has_attachment", "")
    if has_attachment == "yes":
        emails = emails.filter(attachments__isnull=False).distinct()
    elif has_attachment == "no":
        emails = emails.filter(attachments__isnull=True)

    reply_status = request.GET.get("reply_status", "")
    if reply_status == "replied":
        emails = emails.filter(Q(acknowledged=True) | Q(has_reply=True))
    elif reply_status == "not_replied":
        emails = emails.filter(acknowledged=False, has_reply=False)
    return emails.order_by("-date_received", "-id")


@login_required
def inbox(request):
    require_mailprint_operator(request.user)
    page_obj = Paginator(_filtered_emails(request), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "mails/inbox.html",
        {
            "page_obj": page_obj,
            "status_choices": ReceivedEmail.Status.choices,
            "filters": request.GET,
        },
    )


@login_required
def inbox_rows(request):
    require_mailprint_operator(request.user)
    page_obj = Paginator(_filtered_emails(request), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(request, "mails/_inbox_rows.html", {"page_obj": page_obj})


@login_required
def email_detail(request, email_id):
    require_mailprint_operator(request.user)
    email = get_object_or_404(
        ReceivedEmail.objects.prefetch_related(
            "attachments",
            "print_logs",
            Prefetch(
                "acknowledgements",
                queryset=Acknowledgement.objects.order_by(
                    F("sent_at").asc(nulls_last=True),
                    "pk",
                ),
            ),
        ),
        pk=email_id,
    )
    replies = list(email.acknowledgements.all())
    for reply in replies:
        reply.safe_body_html = sanitize_email_html(reply.body_html)

    return render(
        request,
        "mails/detail.html",
        {
            "email": email,
            "email_body_srcdoc": build_email_display_document(
                email.body_html,
                email.body_text,
            ),
            "has_sent_replies": any(
                reply.status == Acknowledgement.Status.SENT
                for reply in replies
            ),
            "has_printable_attachments": any(
                attachment.printable and attachment.file
                for attachment in email.attachments.all()
            ),
            "edats_tracking_url": (
                build_public_tracking_url(
                    email.edats_tracking_number,
                    email.edats_sender_name,
                )
                if email.edats_tracking_number
                else ""
            ),
        },
    )


def _acknowledgement_preview_token(email, template, body):
    return signing.dumps(
        {
            "email_id": email.pk,
            "template_id": template.pk,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
        salt=ACKNOWLEDGEMENT_PREVIEW_SALT,
    )


def _acknowledgement_preview_matches(token, email, template, body):
    try:
        payload = signing.loads(
            token,
            salt=ACKNOWLEDGEMENT_PREVIEW_SALT,
            max_age=15 * 60,
        )
    except signing.BadSignature:
        return False
    expected = {
        "email_id": email.pk,
        "template_id": template.pk,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    return payload == expected


@login_required
def acknowledgement_compose(request, email_id):
    require_mailprint_operator(request.user)
    email = get_object_or_404(ReceivedEmail, pk=email_id)
    form = AcknowledgementSendForm(request.POST or None)
    previewed = False
    preview_body = ""
    preview_recipient = ""
    preview_subject = ""
    preview_token = ""
    duplicate_confirmation_required = False

    if request.method == "POST" and form.is_valid():
        template = form.cleaned_data["template"]
        try:
            configuration, _ = AppConfiguration.objects.get_or_create(key=1)
            preview_message, preview_recipient, preview_body = build_reply_message(
                email,
                template,
                configuration,
            )
            preview_subject = str(preview_message["Subject"])
            action = request.POST.get("action")
            if action == "preview":
                previewed = True
                preview_token = _acknowledgement_preview_token(
                    email, template, preview_body
                )
            elif action == "send":
                preview_token = request.POST.get("preview_token", "")
                if not _acknowledgement_preview_matches(
                    preview_token, email, template, preview_body
                ):
                    messages.error(
                        request,
                        "Preview expired or changed. Review the refreshed preview before sending.",
                    )
                    previewed = True
                    preview_token = _acknowledgement_preview_token(
                        email, template, preview_body
                    )
                else:
                    already_sent = email.acknowledgements.filter(
                        status="sent"
                    ).exists()
                    if already_sent and not form.cleaned_data["confirm_duplicate"]:
                        previewed = True
                        duplicate_confirmation_required = True
                    else:
                        acknowledgement = send_acknowledgement(
                            email,
                            template,
                            user=request.user,
                            confirm_duplicate=form.cleaned_data["confirm_duplicate"],
                        )
                        messages.success(
                            request,
                            f"Acknowledgement sent to {acknowledgement.recipient}.",
                        )
                        return redirect("mails:detail", email_id=email.pk)
            else:
                messages.error(request, "Choose Preview before sending an acknowledgement.")
        except DuplicateAcknowledgementError as exc:
            messages.warning(request, str(exc))
            previewed = True
            preview_token = _acknowledgement_preview_token(
                email, template, preview_body
            )
            duplicate_confirmation_required = True
        except AcknowledgementError as exc:
            messages.error(request, str(exc))
            previewed = bool(request.POST.get("preview_token"))
            preview_token = request.POST.get("preview_token", "")

    return render(
        request,
        "mails/acknowledgement_compose.html",
        {
            "email": email,
            "form": form,
            "previewed": previewed,
            "preview_body": preview_body,
            "preview_recipient": preview_recipient,
            "preview_subject": preview_subject,
            "preview_token": preview_token,
            "duplicate_confirmation_required": duplicate_confirmation_required,
        },
    )


def _edats_preview_token(email, template, tracking_number, body, body_html):
    return signing.dumps(
        {
            "email_id": email.pk,
            "template_id": template.pk,
            "tracking_number": tracking_number,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "html_sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
        },
        salt=EDATS_ACKNOWLEDGEMENT_PREVIEW_SALT,
    )


def _edats_preview_matches(token, email, template, tracking_number, body, body_html):
    try:
        payload = signing.loads(
            token,
            salt=EDATS_ACKNOWLEDGEMENT_PREVIEW_SALT,
            max_age=15 * 60,
        )
    except signing.BadSignature:
        return False
    expected = {
        "email_id": email.pk,
        "template_id": template.pk,
        "tracking_number": tracking_number,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "html_sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
    }
    return payload == expected


@login_required
def edats_acknowledgement_compose(request, email_id):
    require_mailprint_operator(request.user)
    email = get_object_or_404(ReceivedEmail, pk=email_id)
    form = EdatsAcknowledgementSendForm(
        request.POST or None,
        initial={"tracking_number": email.edats_tracking_number},
    )
    previewed = False
    preview_body = ""
    preview_html_srcdoc = ""
    preview_recipient = ""
    preview_subject = ""
    preview_token = ""
    edats_document = None
    duplicate_confirmation_required = False

    if request.method == "POST" and form.is_valid():
        template = form.cleaned_data["template"]
        tracking_number = form.cleaned_data["tracking_number"].strip()
        action = request.POST.get("action")
        if action not in ("verify", "preview", "send"):
            messages.error(
                request,
                "Choose Verify, Preview, or Send acknowledgement.",
            )
        else:
            try:
                edats_document = fetch_edats_document(tracking_number)
                subject_matches = subjects_match(
                    email.subject,
                    edats_document["subject"],
                )
                if not subject_matches:
                    messages.warning(
                        request,
                        "The EDATS subject differs from this email. Review both subjects; you can continue if this is the correct document.",
                    )
                if (
                    email.edats_tracking_number != edats_document["tracking_number"]
                    or email.edats_sender_name != edats_document["sender"]
                ):
                    email.edats_tracking_number = edats_document["tracking_number"]
                    email.edats_sender_name = edats_document["sender"]
                    email.save(
                        update_fields=[
                            "edats_tracking_number",
                            "edats_sender_name",
                        ]
                    )
                    AuditLog.objects.create(
                        user=request.user,
                        action="email_edats_tracking_linked",
                        object_type="ReceivedEmail",
                        object_id=str(email.pk),
                        details={
                            "tracking_number": email.edats_tracking_number,
                            "edats_subject": edats_document["subject"],
                        },
                    )

                if action == "verify":
                    if subject_matches:
                        messages.success(
                            request,
                            f"Verified {tracking_number}; the EDATS subject matches this email.",
                        )
                else:
                    extra_variables = {
                        "tracking_number": edats_document["tracking_number"],
                        "tracking_base_url": edats_document["tracking_base_url"],
                        "tracking_url": edats_document["tracking_url"],
                        "edats_sender": edats_document["sender"],
                    }
                    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
                    preview_message, preview_recipient, preview_body = build_reply_message(
                        email,
                        template,
                        configuration,
                        extra_variables=extra_variables,
                        html_body_template=template.body_html,
                    )
                    preview_subject = str(preview_message["Subject"])
                    preview_html = preview_message.get_body(
                        preferencelist=("html",)
                    ).get_content()
                    preview_html_srcdoc = build_email_display_document(
                        preview_html,
                        "",
                    )
                    if action == "preview":
                        previewed = True
                        preview_token = _edats_preview_token(
                            email,
                            template,
                            tracking_number,
                            preview_body,
                            preview_html,
                        )
                    else:
                        preview_token = request.POST.get("preview_token", "")
                        if not _edats_preview_matches(
                            preview_token,
                            email,
                            template,
                            tracking_number,
                            preview_body,
                            preview_html,
                        ):
                            messages.error(
                                request,
                                "Preview expired or changed. Review the refreshed preview before sending.",
                            )
                            previewed = True
                            preview_token = _edats_preview_token(
                                email,
                                template,
                                tracking_number,
                                preview_body,
                                preview_html,
                            )
                        else:
                            already_sent = email.acknowledgements.filter(
                                status=Acknowledgement.Status.SENT
                            ).exists()
                            if (
                                already_sent
                                and not form.cleaned_data["confirm_duplicate"]
                            ):
                                previewed = True
                                duplicate_confirmation_required = True
                            else:
                                acknowledgement = send_acknowledgement(
                                    email,
                                    template,
                                    user=request.user,
                                    confirm_duplicate=form.cleaned_data[
                                        "confirm_duplicate"
                                    ],
                                    extra_variables=extra_variables,
                                    html_body_template=template.body_html,
                                )
                                messages.success(
                                    request,
                                    "EDATS acknowledgement sent to "
                                    f"{acknowledgement.recipient}.",
                                )
                                return redirect(
                                    "mails:detail",
                                    email_id=email.pk,
                                )
            except EdatsLookupError as exc:
                messages.error(request, str(exc))
            except (AcknowledgementError, ValueError) as exc:
                messages.error(request, str(exc))

    return render(
        request,
        "mails/edats_acknowledgement_compose.html",
        {
            "email": email,
            "form": form,
            "previewed": previewed,
            "preview_body": preview_body,
            "preview_html_srcdoc": preview_html_srcdoc,
            "preview_recipient": preview_recipient,
            "preview_subject": preview_subject,
            "preview_token": preview_token,
            "edats_document": edats_document,
            "edats_subject_matches": bool(
                edats_document
                and subjects_match(email.subject, edats_document["subject"])
            ),
            "duplicate_confirmation_required": duplicate_confirmation_required,
        },
    )


def _cancel_pending_jobs(email):
    email.print_jobs.filter(status=PrintJob.Status.QUEUED).update(
        status=PrintJob.Status.CANCELLED,
        completed_at=timezone.now(),
    )


def _apply_email_action(request, email, action):
    if email.print_jobs.filter(status=PrintJob.Status.PRINTING).exists():
        return "A print job is currently running for this email."

    if action in ("print", "reprint"):
        try:
            create_print_job(
                email,
                requested_by=request.user,
                explicit_reprint=action == "reprint",
                print_replies=request.POST.get("include_replies") == "1",
            )
        except PrintPipelineError as exc:
            return str(exc)
        return None

    if action == "print_attachments":
        if not email.attachments.filter(printable=True).exclude(file="").exists():
            return "This email has no saved printable attachments."
        try:
            create_print_job(
                email,
                requested_by=request.user,
                print_email_body=False,
                print_attachments=True,
                explicit_reprint=True,
            )
        except PrintPipelineError as exc:
            return str(exc)
        return None

    if action == "mark_printed":
        _cancel_pending_jobs(email)
        email.status = ReceivedEmail.Status.PRINTED
        email.error_message = ""
        email.save(update_fields=["status", "error_message"])
        PrintLog.objects.create(
            email=email,
            filename="",
            printer_name="",
            result=PrintLog.Result.MANUAL,
            details="Marked as printed without sending a print job.",
            user=request.user,
        )
        return None

    if action == "mark_printing":
        _cancel_pending_jobs(email)
        email.status = ReceivedEmail.Status.PRINTING
        email.save(update_fields=["status"])
        return None

    if action == "reset_status":
        _cancel_pending_jobs(email)
        email.status = ReceivedEmail.Status.NEW
        email.error_message = ""
        email.save(update_fields=["status", "error_message"])
        return None

    if action == "hold":
        _cancel_pending_jobs(email)
        email.status = ReceivedEmail.Status.HELD
        email.save(update_fields=["status"])
        return None

    if action in ("skip", "ignore"):
        _cancel_pending_jobs(email)
        email.status = ReceivedEmail.Status.SKIPPED
        email.save(update_fields=["status"])
        return None

    return f"Unsupported email action: {action}"


@login_required
@require_POST
def email_action(request, email_id, action):
    require_mailprint_operator(request.user)
    if not is_mailprint_admin(request.user) and action not in (
        "print",
        "reprint",
        "print_attachments",
    ):
        raise PermissionDenied
    email = get_object_or_404(ReceivedEmail, pk=email_id)
    if action == "delete":
        if email.print_jobs.filter(status=PrintJob.Status.PRINTING).exists():
            messages.error(request, "A printing email cannot be deleted.")
            return redirect("mails:detail", email_id=email.pk)
        filenames = [
            field.name
            for field in (email.raw_email_file, email.body_pdf_file)
            if field
        ]
        filenames.extend(
            name for name in email.attachments.values_list("file", flat=True) if name
        )
        email_id_value = email.pk
        email.delete()
        for filename in filenames:
            default_storage.delete(filename)
        AuditLog.objects.create(
            user=request.user,
            action="email_deleted",
            object_type="ReceivedEmail",
            object_id=str(email_id_value),
        )
        messages.success(request, "Email and stored files deleted.")
        return redirect("mails:inbox")

    error = _apply_email_action(request, email, action)
    if error:
        messages.error(request, error)
    else:
        AuditLog.objects.create(
            user=request.user,
            action=f"email_{action}",
            object_type="ReceivedEmail",
            object_id=str(email.pk),
        )
        messages.success(request, "Email action completed.")
    return redirect("mails:detail", email_id=email.pk)


@login_required
@require_POST
def bulk_action(request):
    require_mailprint_operator(request.user)
    action = request.POST.get("action", "")
    if not is_mailprint_admin(request.user) and action not in ("print", "reprint"):
        raise PermissionDenied
    email_ids = request.POST.getlist("selected_ids")
    if not email_ids:
        messages.warning(request, "Select at least one email.")
        return redirect("mails:inbox")

    emails = ReceivedEmail.objects.filter(pk__in=email_ids)
    completed = 0
    errors = []
    for email in emails:
        error = _apply_email_action(request, email, action)
        if error:
            errors.append(f"{email.subject or email.pk}: {error}")
        else:
            completed += 1
            AuditLog.objects.create(
                user=request.user,
                action=f"email_{action}",
                object_type="ReceivedEmail",
                object_id=str(email.pk),
            )
    if completed:
        messages.success(request, f"Action applied to {completed} email(s).")
    for error in errors[:5]:
        messages.error(request, error)
    return redirect("mails:inbox")


def _email_file_response(field, filename, content_type, *, as_attachment):
    if not field:
        raise Http404("File is not available.")
    try:
        file_handle = field.open("rb")
    except (OSError, ValueError):
        raise Http404("File is not available.") from None
    response = FileResponse(
        file_handle,
        as_attachment=as_attachment,
        filename=filename,
        content_type=content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if not as_attachment:
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@login_required
def download_attachment(request, attachment_id):
    require_mailprint_operator(request.user)
    attachment = get_object_or_404(EmailAttachment, pk=attachment_id)
    return _email_file_response(
        attachment.file,
        attachment.filename,
        "application/octet-stream",
        as_attachment=True,
    )


def _client_print_pdf_response(pdf_file, filename):
    response = FileResponse(
        pdf_file,
        as_attachment=False,
        filename=filename,
        content_type="application/pdf",
    )
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "sandbox allow-modals; default-src 'none'"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


@login_required
def client_print_email(request, email_id):
    require_mailprint_operator(request.user)
    email = get_object_or_404(ReceivedEmail, pk=email_id)
    try:
        pdf = render_email_to_pdf(
            email,
            include_replies=request.GET.get("include_replies") == "1",
        )
    except ConversionError as exc:
        messages.error(request, f"Could not prepare the email PDF: {exc}")
        return redirect("mails:detail", email_id=email.pk)

    return _client_print_pdf_response(
        BytesIO(pdf),
        f"email-{email.pk}.pdf",
    )


@login_required
def client_print_attachment(request, attachment_id):
    require_mailprint_operator(request.user)
    attachment = get_object_or_404(
        EmailAttachment,
        pk=attachment_id,
        printable=True,
    )
    if not attachment.file:
        raise Http404("Attachment file is not available.")

    pdf_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with tempfile.TemporaryDirectory(prefix="mailprint-client-") as directory:
            pdf_path = convert_attachment_to_pdf(attachment, directory)
            with pdf_path.open("rb") as source:
                shutil.copyfileobj(source, pdf_file)
    except (ConversionError, OSError, ValueError) as exc:
        pdf_file.close()
        messages.error(request, f"Could not prepare the attachment PDF: {exc}")
        return redirect("mails:detail", email_id=attachment.email_id)

    pdf_file.seek(0)
    return _client_print_pdf_response(
        pdf_file,
        f"attachment-{attachment.pk}.pdf",
    )


@login_required
def preview_attachment(request, attachment_id):
    require_mailprint_operator(request.user)
    attachment = get_object_or_404(EmailAttachment, pk=attachment_id)
    content_type = attachment.content_type.lower()
    if content_type != "application/pdf" and not content_type.startswith("image/"):
        raise Http404("This attachment type cannot be previewed.")
    return _email_file_response(
        attachment.file,
        attachment.filename,
        content_type,
        as_attachment=False,
    )


@login_required
def download_raw_email(request, email_id):
    require_mailprint_operator(request.user)
    email = get_object_or_404(ReceivedEmail, pk=email_id)
    return _email_file_response(
        email.raw_email_file,
        "message.eml",
        "message/rfc822",
        as_attachment=True,
    )
