import time

from django.db import transaction

from .models import PrinterProfile


class PrinterManagementError(Exception):
    pass


def wait_for_printer_queue_empty(
    printer_name,
    *,
    timeout_seconds=300,
    poll_interval_seconds=1,
):
    win32print, pywintypes = _win32_modules()
    deadline = time.monotonic() + timeout_seconds
    while True:
        handle = None
        try:
            handle = win32print.OpenPrinter(printer_name)
            job_count = int(win32print.GetPrinter(handle, 2).get("cJobs") or 0)
            jobs = win32print.EnumJobs(handle, 0, job_count, 2) if job_count else []
        except pywintypes.error as exc:
            raise PrinterManagementError(
                f"Could not check the print queue for {printer_name!r}: {exc}"
            ) from exc
        finally:
            if handle is not None:
                try:
                    win32print.ClosePrinter(handle)
                except pywintypes.error as exc:
                    raise PrinterManagementError(
                        f"Could not close the printer queue for {printer_name!r}: {exc}"
                    ) from exc

        terminal_statuses = (
            getattr(win32print, "JOB_STATUS_PRINTED", 0)
            | getattr(win32print, "JOB_STATUS_DELETED", 0)
        )
        pending_jobs = [
            job
            for job in jobs
            if not int(job.get("Status") or 0) & terminal_statuses
            and "printed" not in str(job.get("pStatus") or "").casefold()
            and "deleted" not in str(job.get("pStatus") or "").casefold()
        ]
        if not pending_jobs:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PrinterManagementError(
                f"Timed out waiting for the print queue on {printer_name!r} to clear."
            )
        time.sleep(min(poll_interval_seconds, remaining))


def _win32_modules():
    try:
        import pywintypes
        import win32print
    except ImportError as exc:
        raise PrinterManagementError(
            "Windows printer management requires pywin32."
        ) from exc
    return win32print, pywintypes


def _printer_state(status, win32print):
    errors = (
        ("ERROR", "Error"),
        ("PAPER_JAM", "Paper jam"),
        ("PAPER_OUT", "Paper out"),
        ("PAPER_PROBLEM", "Paper problem"),
        ("DOOR_OPEN", "Door open"),
        ("USER_INTERVENTION", "User intervention required"),
        ("OUT_OF_MEMORY", "Printer out of memory"),
        ("TONER_LOW", "Toner low"),
    )
    offline = (
        ("OFFLINE", "Offline"),
        ("NOT_AVAILABLE", "Not available"),
        ("PAUSED", "Paused"),
        ("PENDING_DELETION", "Pending deletion"),
    )
    error_details = [
        label
        for flag_name, label in errors
        if status & getattr(win32print, f"PRINTER_STATUS_{flag_name}", 0)
    ]
    offline_details = [
        label
        for flag_name, label in offline
        if status & getattr(win32print, f"PRINTER_STATUS_{flag_name}", 0)
    ]
    if error_details:
        return "error", ", ".join(error_details)
    if offline_details:
        return "offline", ", ".join(offline_details)
    if status & getattr(win32print, "PRINTER_STATUS_PRINTING", 0):
        return "ready", "Printing"
    return "ready", "Ready"


def list_installed_printers():
    win32print, pywintypes = _win32_modules()
    try:
        printer_records = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS,
            None,
            2,
        )
    except pywintypes.error as exc:
        raise PrinterManagementError(f"Could not list Windows printers: {exc}") from exc

    try:
        default_printer = win32print.GetDefaultPrinter()
    except pywintypes.error:
        default_printer = ""
    profiles = {
        profile.printer_name: profile
        for profile in PrinterProfile.objects.all()
    }

    printers = []
    seen_names = set()
    for record in printer_records:
        name = record.get("pPrinterName", "")
        if not name:
            continue
        seen_names.add(name)
        profile = profiles.get(name)
        status, status_detail = _printer_state(int(record.get("Status") or 0), win32print)
        printers.append(
            {
                "name": name,
                "alias": profile.alias if profile else "",
                "is_default": bool(profile and profile.is_default),
                "is_system_default": name == default_printer,
                "dedicated_purpose": profile.dedicated_purpose if profile else "",
                "status": status,
                "status_detail": status_detail,
                "job_count": int(record.get("cJobs") or 0),
                "profile": profile,
                "installed": True,
            }
        )

    for name, profile in profiles.items():
        if name in seen_names:
            continue
        printers.append(
            {
                "name": name,
                "alias": profile.alias,
                "is_default": profile.is_default,
                "is_system_default": name == default_printer,
                "dedicated_purpose": profile.dedicated_purpose,
                "status": "offline",
                "status_detail": "Not installed or unavailable",
                "job_count": 0,
                "profile": profile,
                "installed": False,
            }
        )

    return sorted(printers, key=lambda printer: (printer["alias"].casefold(), printer["name"].casefold()))


def list_printer_jobs(printer_name, *, limit=50):
    win32print, pywintypes = _win32_modules()
    handle = None
    try:
        handle = win32print.OpenPrinter(printer_name)
        jobs = win32print.EnumJobs(handle, 0, limit, 2)
        return [
            {
                "job_id": int(job.get("JobId") or 0),
                "document": job.get("pDocument") or "(untitled)",
                "user": job.get("pUserName") or "",
                "status": job.get("pStatus") or job.get("StatusString") or "Queued",
                "pages_printed": int(job.get("PagesPrinted") or 0),
                "total_pages": int(job.get("TotalPages") or 0),
            }
            for job in jobs
        ]
    except pywintypes.error as exc:
        raise PrinterManagementError(
            f"Could not read the queue for {printer_name!r}: {exc}"
        ) from exc
    finally:
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except pywintypes.error as exc:
                raise PrinterManagementError(
                    f"Could not close the Windows printer handle: {exc}"
                ) from exc


def cancel_printer_job(printer_name, job_id):
    if job_id <= 0:
        raise PrinterManagementError("A valid Windows print job ID is required.")
    win32print, pywintypes = _win32_modules()
    handle = None
    try:
        handle = win32print.OpenPrinter(printer_name)
        win32print.SetJob(
            handle,
            job_id,
            0,
            None,
            win32print.JOB_CONTROL_CANCEL,
        )
    except pywintypes.error as exc:
        raise PrinterManagementError(
            f"Could not cancel job {job_id} on {printer_name!r}: {exc}"
        ) from exc
    finally:
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except pywintypes.error as exc:
                raise PrinterManagementError(
                    f"Could not close the Windows printer handle: {exc}"
                ) from exc


def save_printer_profile(profile):
    with transaction.atomic():
        if profile.is_default:
            PrinterProfile.objects.exclude(pk=profile.pk).update(is_default=False)
        profile.save()
    return profile
