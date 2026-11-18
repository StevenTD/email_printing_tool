import hashlib
import shutil
import tempfile
from pathlib import Path

from decouple import config
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import require_mailprint_admin
from core.models import AppConfiguration, AuditLog, WorkerLog
from mails.models import ReceivedEmail

from .conversion import ConversionError, render_email_to_pdf
from .forms import PrinterProfileForm, SumatraPathForm
from .locks import printer_mutex
from .models import PrinterProfile
from .pipeline import PrintPipelineError, _send_pdf_to_printer
from .printers import (
    PrinterManagementError,
    cancel_printer_job,
    list_installed_printers,
    list_printer_jobs,
)


def _require_admin(request):
    require_mailprint_admin(request.user)


def _installed_printer_names():
    return [
        printer["name"]
        for printer in list_installed_printers()
        if printer["installed"]
    ]


@login_required
def printer_list(request):
    _require_admin(request)
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    printers = []
    inventory_error = ""
    try:
        printers = list_installed_printers()
    except PrinterManagementError as exc:
        inventory_error = str(exc)

    for printer in printers:
        printer["jobs"] = []
        printer["queue_error"] = ""
        if printer["installed"]:
            try:
                printer["jobs"] = list_printer_jobs(printer["name"])
            except PrinterManagementError as exc:
                printer["queue_error"] = str(exc)

    return render(
        request,
        "printing/printers.html",
        {
            "printers": printers,
            "inventory_error": inventory_error,
            "configuration": configuration,
            "sumatra_form": SumatraPathForm(instance=configuration),
        },
    )


@login_required
@require_POST
def sumatra_settings(request):
    _require_admin(request)
    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    form = SumatraPathForm(request.POST, instance=configuration)
    if not form.is_valid():
        messages.error(request, "SumatraPDF setting was not saved; check the path.")
        return redirect("printing:printers")
    form.save()
    AuditLog.objects.create(
        user=request.user,
        action="sumatra_path_updated",
        object_type="AppConfiguration",
        object_id="1",
        details={"sumatra_pdf_path": configuration.sumatra_pdf_path},
    )
    messages.success(request, "SumatraPDF path saved.")
    return redirect("printing:printers")


@login_required
def printer_profile_create(request):
    _require_admin(request)
    try:
        printer_names = _installed_printer_names()
    except PrinterManagementError as exc:
        printer_names = []
        messages.error(request, str(exc))
    form = PrinterProfileForm(
        request.POST or None,
        printer_names=printer_names,
        initial={"printer_name": request.GET.get("printer_name", "")},
    )
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        AuditLog.objects.create(
            user=request.user,
            action="printer_profile_created",
            object_type="PrinterProfile",
            object_id=str(profile.pk),
            details={"printer_name": profile.printer_name},
        )
        messages.success(request, "Printer profile saved.")
        return redirect("printing:printers")
    return render(
        request,
        "printing/printer_form.html",
        {"form": form, "title": "Configure printer"},
    )


@login_required
def printer_profile_edit(request, profile_id):
    _require_admin(request)
    profile = get_object_or_404(PrinterProfile, pk=profile_id)
    try:
        printer_names = _installed_printer_names()
    except PrinterManagementError as exc:
        printer_names = []
        messages.error(request, str(exc))
    form = PrinterProfileForm(
        request.POST or None,
        instance=profile,
        printer_names=printer_names,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.objects.create(
            user=request.user,
            action="printer_profile_updated",
            object_type="PrinterProfile",
            object_id=str(profile.pk),
            details={"printer_name": profile.printer_name},
        )
        messages.success(request, "Printer profile updated.")
        return redirect("printing:printers")
    return render(
        request,
        "printing/printer_form.html",
        {"form": form, "title": f"Configure {profile}"},
    )


@login_required
@require_POST
def printer_test_page(request):
    _require_admin(request)
    printer_name = request.POST.get("printer_name", "")
    try:
        printer = next(
            (
                item
                for item in list_installed_printers()
                if item["name"] == printer_name and item["installed"]
            ),
            None,
        )
        if printer is None:
            raise PrinterManagementError("Select an installed printer to print a test page.")

        configuration, _ = AppConfiguration.objects.get_or_create(key=1)
        test_email = ReceivedEmail(
            subject="MailPrint printer test",
            body_text=(
                f"Printer: {printer_name}\n"
                f"Generated: {timezone.localtime():%Y-%m-%d %H:%M:%S %Z}\n"
                "This test page verifies MailPrint PDF rendering and printer selection."
            ),
        )
        pdf_bytes = render_email_to_pdf(test_email)
        profile = PrinterProfile.objects.filter(printer_name=printer_name).first()

        with tempfile.TemporaryDirectory(prefix="mailprint-test-") as directory:
            pdf_path = Path(directory) / "mailprint-test-page.pdf"
            pdf_path.write_bytes(pdf_bytes)
            if configuration.dry_run_enabled:
                output_directory = Path(
                    config("MAILPRINT_DRY_RUN_DIR", default="").strip()
                    or settings.BASE_DIR / "dry_run_output"
                )
                output_directory = output_directory / "test_pages"
                output_directory.mkdir(parents=True, exist_ok=True)
                printer_hash = hashlib.sha256(
                    printer_name.encode("utf-8")
                ).hexdigest()[:12]
                target = output_directory / f"{printer_hash}.pdf"
                shutil.copy2(pdf_path, target)
                result = "dry_run"
                details = f"Saved test page to {target}"
            else:
                with printer_mutex(printer_name):
                    _send_pdf_to_printer(
                        pdf_path,
                        printer_name,
                        1,
                        configuration.sumatra_pdf_path,
                        profile=profile,
                    )
                result = "printed"
                details = "SumatraPDF accepted the test page print request."

        AuditLog.objects.create(
            user=request.user,
            action="printer_test_page",
            object_type="PrinterProfile",
            object_id=str(profile.pk) if profile else printer_name,
            details={"printer_name": printer_name, "result": result, "details": details},
        )
        messages.success(
            request,
            "Test page saved to the dry-run folder."
            if result == "dry_run"
            else f"Test page sent to {printer_name}.",
        )
    except (PrinterManagementError, ConversionError, PrintPipelineError, OSError, RuntimeError) as exc:
        WorkerLog.objects.create(
            level=WorkerLog.Level.ERROR,
            component="printing",
            message=f"Printer test page failed for {printer_name!r}: {exc}",
        )
        AuditLog.objects.create(
            user=request.user,
            action="printer_test_page_failed",
            object_type="PrinterProfile",
            object_id=printer_name,
            details={"error": str(exc)},
        )
        messages.error(request, f"Could not print the test page: {exc}")
    return redirect("printing:printers")


@login_required
@require_POST
def printer_job_cancel(request):
    _require_admin(request)
    printer_name = request.POST.get("printer_name", "")
    try:
        installed = any(
            printer["name"] == printer_name and printer["installed"]
            for printer in list_installed_printers()
        )
        if not installed:
            raise PrinterManagementError("The selected printer is not currently installed.")
        job_id = int(request.POST.get("job_id", ""))
        cancel_printer_job(printer_name, job_id)
    except (PrinterManagementError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        AuditLog.objects.create(
            user=request.user,
            action="printer_job_cancelled",
            object_type="WindowsPrintJob",
            object_id=str(job_id),
            details={"printer_name": printer_name},
        )
        messages.success(request, f"Windows print job {job_id} cancelled.")
    return redirect("printing:printers")
