from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.access import require_mailprint_admin, require_mailprint_operator
from core.models import AppConfiguration, AuditLog, WorkerHeartbeat, WorkerLog
from mails.models import ReceivedEmail
from printing.models import PrintJob, PrintLog


def _dashboard_stats():
    today = timezone.localdate()
    configuration = AppConfiguration.objects.order_by("key").first()
    worker = WorkerHeartbeat.objects.order_by("key").first()
    return {
        "new_count": ReceivedEmail.objects.filter(status=ReceivedEmail.Status.NEW).count(),
        "held_count": ReceivedEmail.objects.filter(status=ReceivedEmail.Status.HELD).count(),
        "printed_today_count": (
            PrintLog.objects.filter(
                result=PrintLog.Result.SUCCESS,
                printed_at__date=today,
            )
            .values("email_id")
            .distinct()
            .count()
        ),
        "failed_count": ReceivedEmail.objects.filter(status=ReceivedEmail.Status.FAILED).count(),
        "last_fetch_at": worker.last_fetch_at if worker else None,
        "worker_running": worker.running if worker else False,
        "auto_print_enabled": configuration.auto_print_enabled if configuration else False,
    }


@login_required
def dashboard(request):
    require_mailprint_operator(request.user)
    context = _dashboard_stats()
    context["recent_activity"] = WorkerLog.objects.select_related("email")[:10]
    return render(request, "core/dashboard.html", context)


@login_required
def dashboard_stats(request):
    require_mailprint_operator(request.user)
    return render(request, "core/_dashboard_stats.html", _dashboard_stats())


@login_required
@require_POST
def toggle_auto_print(request):
    require_mailprint_admin(request.user)
    requested_state = request.POST.get("enabled")
    if requested_state not in ("true", "false"):
        return HttpResponseBadRequest("enabled must be true or false")

    configuration, _ = AppConfiguration.objects.get_or_create(key=1)
    configuration.auto_print_enabled = requested_state == "true"
    configuration.save(update_fields=["auto_print_enabled", "updated_at"])
    if not configuration.auto_print_enabled:
        now = timezone.now()
        pending_jobs = PrintJob.objects.filter(
            automatic=True,
            status=PrintJob.Status.QUEUED,
        )
        affected_email_ids = list(pending_jobs.values_list("email_id", flat=True))
        pending_jobs.update(
            status=PrintJob.Status.CANCELLED,
            completed_at=now,
        )
        ReceivedEmail.objects.filter(
            pk__in=affected_email_ids,
            status=ReceivedEmail.Status.QUEUED,
        ).update(status=ReceivedEmail.Status.HELD)
    AuditLog.objects.create(
        user=request.user,
        action="auto_print_enabled" if configuration.auto_print_enabled else "auto_print_disabled",
        object_type="AppConfiguration",
        object_id="1",
        details={"enabled": configuration.auto_print_enabled},
    )
    state = "ON" if configuration.auto_print_enabled else "OFF"
    messages.success(request, f"Auto-print is now {state}.")

    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(reverse("core:dashboard"))
