from django.core.exceptions import PermissionDenied


ADMIN_GROUP_NAME = "MailPrint Admin"
OPERATOR_GROUP_NAME = "MailPrint Operator"


def is_mailprint_admin(user):
    if not user.is_authenticated:
        return False
    return (
        user.is_superuser
        or user.is_staff
        or user.groups.filter(name=ADMIN_GROUP_NAME).exists()
    )


def is_mailprint_operator(user):
    if not user.is_authenticated:
        return False
    return is_mailprint_admin(user) or user.groups.filter(
        name=OPERATOR_GROUP_NAME
    ).exists()


def require_mailprint_admin(user):
    if not is_mailprint_admin(user):
        raise PermissionDenied


def require_mailprint_operator(user):
    if not is_mailprint_operator(user):
        raise PermissionDenied
