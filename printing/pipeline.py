import shutil
import subprocess
import tempfile
from pathlib import Path

from decouple import config
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.models import AppConfiguration, WorkerLog
from mails.models import EmailAttachment, ReceivedEmail

from .conversion import ConversionError, convert_attachment_to_pdf, render_email_to_pdf
from .locks import printer_mutex
from .models import PrintJob, PrintLog, PrinterProfile
from .printers import PrinterManagementError, wait_for_printer_queue_empty


class PrintPipelineError(Exception):
    pass


class AlreadyPrintedError(PrintPipelineError):
    pass


def create_print_job(
    email,
    *,
    printer_name="",
    copies=1,
    print_email_body=True,
    print_replies=False,
    print_attachments=True,
    requested_by=None,
    explicit_reprint=False,
    automatic=False,
):
    if print_replies and not print_email_body:
        raise PrintPipelineError("Replies can only be included when printing the email body.")
    if email.status == ReceivedEmail.Status.PRINTED and not explicit_reprint:
        raise PrintPipelineError("This email is already printed; request an explicit reprint.")
    if email.print_jobs.filter(
        status__in=[PrintJob.Status.QUEUED, PrintJob.Status.PRINTING]
    ).exists():
        raise PrintPipelineError("This email already has an active print job.")
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    job = PrintJob.objects.create(
        email=email,
        prior_email_status=email.status,
        printer_name=printer_name,
        copies=copies,
        print_email_body=print_email_body,
        print_replies=print_replies,
        print_attachments=print_attachments,
        requested_by=requested_by,
        explicit_reprint=explicit_reprint,
        automatic=automatic,
        dry_run=configuration.dry_run_enabled,
    )
    email.status = ReceivedEmail.Status.QUEUED
    email.save(update_fields=["status"])
    return job


def process_next_job():
    with transaction.atomic():
        job = (
            PrintJob.objects.select_for_update()
            .filter(status=PrintJob.Status.QUEUED)
            .order_by("created_at", "id")
            .first()
        )
        if job is None:
            return None
        email = ReceivedEmail.objects.select_for_update().get(pk=job.email_id)
        previous_email_status = email.status
        job.status = PrintJob.Status.PRINTING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])
        email.status = ReceivedEmail.Status.PRINTING
        email.save(update_fields=["status"])

    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    dry_run = job.dry_run or configuration.dry_run_enabled
    job.dry_run = dry_run
    job.save(update_fields=["dry_run"])

    try:
        printer_name = _resolve_printer(job) if not dry_run else job.printer_name
        job.printer_name = printer_name
        job.save(update_fields=["printer_name"])
        if dry_run:
            completed_files = _print_job_files(
                job,
                email,
                configuration,
                printer_name,
                dry_run,
            )
        else:
            with printer_mutex(printer_name):
                if (
                    not job.explicit_reprint
                    and email.print_logs.filter(result=PrintLog.Result.SUCCESS).exists()
                ):
                    raise AlreadyPrintedError(
                        "This email has already been printed; an explicit reprint is required."
                    )
                completed_files = _print_job_files(
                    job,
                    email,
                    configuration,
                    printer_name,
                    dry_run,
                )
        if not completed_files:
            raise PrintPipelineError("No printable documents were selected for this job.")
    except Exception as exc:
        job.status = PrintJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        if isinstance(exc, AlreadyPrintedError):
            email.refresh_from_db()
            if email.status == ReceivedEmail.Status.PRINTING:
                prior_status = job.prior_email_status or previous_email_status
                if prior_status == ReceivedEmail.Status.FAILED:
                    email.status = prior_status
                elif email.print_logs.filter(result=PrintLog.Result.SUCCESS).exists():
                    email.status = ReceivedEmail.Status.PRINTED
                else:
                    email.status = prior_status
                email.error_message = ""
                email.save(update_fields=["status", "error_message"])
        else:
            email.status = ReceivedEmail.Status.FAILED
            email.error_message = str(exc)
            email.save(update_fields=["status", "error_message"])
        WorkerLog.objects.create(
            level=WorkerLog.Level.ERROR,
            component="printing",
            message=f"Print job {job.pk} failed: {exc}",
            email=email,
        )
        return job

    job.status = PrintJob.Status.DRY_RUN if dry_run else PrintJob.Status.PRINTED
    job.completed_at = timezone.now()
    job.error_message = ""
    job.save(update_fields=["status", "completed_at", "error_message"])
    if dry_run:
        email.status = job.prior_email_status or previous_email_status
    else:
        email.status = ReceivedEmail.Status.PRINTED
        email.error_message = ""
    email.save(update_fields=["status", "error_message"])
    if not dry_run and configuration.auto_acknowledge_after_print:
        from mails.acknowledgements import send_auto_acknowledgement

        send_auto_acknowledgement(email)
    return job


def _print_job_files(job, email, configuration, printer_name, dry_run):
    outputs = []
    failures = []
    profile = PrinterProfile.objects.filter(printer_name=printer_name).first()
    with tempfile.TemporaryDirectory(prefix=f"mailprint-{job.pk}-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        if job.print_email_body:
            try:
                body_pdf = render_email_to_pdf(
                    email,
                    include_replies=job.print_replies,
                )
                email.body_pdf_file.save("body.pdf", ContentFile(body_pdf), save=True)
                outputs.append(("Email body.pdf", Path(email.body_pdf_file.path)))
            except Exception as exc:
                _record_file_result(job, email, "Email body.pdf", printer_name, PrintLog.Result.FAILURE, str(exc))
                failures.append(str(exc))

        if job.print_attachments:
            for index, attachment in enumerate(email.attachments.all(), start=1):
                if not attachment.printable:
                    _record_file_result(
                        job,
                        email,
                        attachment.filename,
                        printer_name,
                        PrintLog.Result.SKIPPED,
                        "Attachment type is not printable.",
                    )
                    continue
                try:
                    attachment_output = temporary_path / f"attachment-{index}"
                    attachment_output.mkdir(parents=True, exist_ok=True)
                    pdf_path = convert_attachment_to_pdf(
                        attachment,
                        attachment_output,
                    )
                    outputs.append((attachment.filename, pdf_path))
                except (ConversionError, OSError, ValueError) as exc:
                    attachment.conversion_error = str(exc)
                    attachment.save(update_fields=["conversion_error"])
                    _record_file_result(
                        job,
                        email,
                        attachment.filename,
                        printer_name,
                        PrintLog.Result.FAILURE,
                        str(exc),
                    )
                    failures.append(str(exc))

        if dry_run:
            configured_output_directory = config("MAILPRINT_DRY_RUN_DIR", default="").strip()
            output_directory = Path(
                configured_output_directory or settings.BASE_DIR / "dry_run_output"
            )
            output_directory = output_directory / f"job-{job.pk}"
            output_directory.mkdir(parents=True, exist_ok=True)
        for index, (filename, pdf_path) in enumerate(outputs, start=1):
            try:
                if not dry_run and index > 1:
                    wait_for_printer_queue_empty(printer_name)
                if dry_run:
                    target_stem = Path(_safe_filename(filename)).stem
                    target = output_directory / f"{index:02d}-{target_stem}.pdf"
                    shutil.copy2(pdf_path, target)
                    result = PrintLog.Result.DRY_RUN
                    details = f"Saved test output to {target}"
                else:
                    _send_pdf_to_printer(
                        pdf_path,
                        printer_name,
                        job.copies,
                        configuration.sumatra_pdf_path,
                        profile=profile,
                    )
                    result = PrintLog.Result.SUCCESS
                    details = f"SumatraPDF accepted the print request ({job.copies} copies)."
                _record_file_result(job, email, filename, printer_name, result, details)
            except (
                OSError,
                subprocess.SubprocessError,
                PrintPipelineError,
                PrinterManagementError,
            ) as exc:
                _record_file_result(
                    job,
                    email,
                    filename,
                    printer_name,
                    PrintLog.Result.FAILURE,
                    str(exc),
                )
                failures.append(str(exc))
        if failures:
            raise PrintPipelineError("; ".join(failures))
    return len(outputs)


def _safe_filename(filename):
    safe = Path(filename.replace("\\", "/")).name
    safe = "".join(character if character.isalnum() or character in " ._-" else "_" for character in safe)
    return safe.strip(" .") or "document"


def _resolve_printer(job):
    if job.printer_name:
        return job.printer_name
    profile = PrinterProfile.objects.filter(is_default=True).first()
    if profile:
        return profile.printer_name
    try:
        import win32print

        return win32print.GetDefaultPrinter()
    except (ImportError, RuntimeError) as exc:
        raise PrintPipelineError(
            "No application default printer is configured and Windows returned no default printer."
        ) from exc


def _send_pdf_to_printer(
    pdf_path,
    printer_name,
    copies,
    configured_sumatra_path,
    *,
    profile=None,
):
    executable = configured_sumatra_path or config(
        "SUMATRA_PDF_PATH",
        default="SumatraPDF.exe",
    )
    resolved_executable = shutil.which(executable) if not Path(executable).is_absolute() else executable
    if not resolved_executable or (Path(executable).is_absolute() and not Path(executable).is_file()):
        raise PrintPipelineError(
            "SumatraPDF was not found. Set SUMATRA_PDF_PATH or the application setting."
        )
    command = [
        resolved_executable,
        "-print-to",
        printer_name,
        "-silent",
    ]
    print_settings = []
    if copies > 1:
        print_settings.append(f"{copies}x")
    if profile:
        if profile.paper_size:
            print_settings.append(f"paper={profile.paper_size}")
        print_settings.append("duplex" if profile.duplex else "simplex")
        if profile.color_mode == PrinterProfile.ColorMode.MONO:
            print_settings.append("monochrome")
        elif profile.color_mode == PrinterProfile.ColorMode.COLOR:
            print_settings.append("color")
    print_settings.append("fit")
    if print_settings:
        command.extend(["-print-settings", ",".join(print_settings)])
    command.append(str(pdf_path))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrintPipelineError(f"SumatraPDF failed to print {Path(pdf_path).name!r}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no SumatraPDF output").strip()
        raise PrintPipelineError(
            f"SumatraPDF returned {result.returncode} for {Path(pdf_path).name!r}: {detail[:1000]}"
        )


def _record_file_result(job, email, filename, printer_name, result, details):
    PrintLog.objects.create(
        email=email,
        job=job,
        filename=filename[:500],
        printer_name=printer_name,
        result=result,
        details=details,
        user=job.requested_by,
    )
