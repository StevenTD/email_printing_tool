from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .models import AuditLog


@receiver(user_logged_in, dispatch_uid="mailprint_audit_login")
def audit_login(sender, request, user, **kwargs):
    AuditLog.objects.create(
        user=user,
        action="login_succeeded",
        object_type="User",
        object_id=str(user.pk),
        details={"remote_addr": request.META.get("REMOTE_ADDR", "") if request else ""},
    )


@receiver(user_logged_out, dispatch_uid="mailprint_audit_logout")
def audit_logout(sender, request, user, **kwargs):
    if user is None:
        return
    AuditLog.objects.create(
        user=user,
        action="logout",
        object_type="User",
        object_id=str(user.pk),
        details={"remote_addr": request.META.get("REMOTE_ADDR", "") if request else ""},
    )
