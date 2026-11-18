from .access import is_mailprint_admin, is_mailprint_operator
from .models import AppConfiguration


def mailprint_status(request):
    configuration = AppConfiguration.objects.order_by("key").first()
    return {
        "mailprint_auto_print_enabled": (
            configuration.auto_print_enabled if configuration else False
        ),
        "mailprint_can_configure": is_mailprint_admin(request.user),
        "mailprint_can_operate": is_mailprint_operator(request.user),
        "mailprint_is_admin": is_mailprint_admin(request.user),
    }
