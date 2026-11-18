from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.access import ADMIN_GROUP_NAME, OPERATOR_GROUP_NAME
from rules.models import AcknowledgementTemplate, FilterRule


class Command(BaseCommand):
    help = "Create MailPrint roles, sample rules/templates, and an administrator."

    def add_arguments(self, parser):
        parser.add_argument(
            "--admin-username",
            default="admin",
            help="Username for the initial administrator (default: admin).",
        )
        parser.add_argument(
            "--skip-admin",
            action="store_true",
            help="Create roles and sample data without prompting for an administrator.",
        )

    def handle(self, *args, **options):
        admin_group, _ = Group.objects.get_or_create(name=ADMIN_GROUP_NAME)
        Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        if not options["skip_admin"]:
            self._ensure_admin(admin_group, options["admin_username"])
        self._create_sample_data()
        self.stdout.write(self.style.SUCCESS("MailPrint roles and sample data are ready."))

    def _ensure_admin(self, admin_group, username):
        user_model = get_user_model()
        existing_user = user_model.objects.filter(username=username).first()
        if existing_user:
            if not (existing_user.is_staff or existing_user.is_superuser):
                raise CommandError(
                    f"User {username!r} exists but is not staff. Choose another "
                    "administrator name or promote this account explicitly."
                )
            admin_group.user_set.add(existing_user)
            self.stdout.write(f"Added existing staff user {username!r} to the Admin role.")
            return

        email = input("Administrator email address: ").strip()
        password = getpass("Administrator password: ")
        confirmation = getpass("Confirm administrator password: ")
        if password != confirmation:
            raise CommandError("The password entries did not match.")
        if not email:
            raise CommandError("An email address is required for the initial administrator.")
        candidate = user_model(username=username, email=email)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        user = user_model.objects.create_superuser(
            username=username,
            email=email,
            password=password,
        )
        admin_group.user_set.add(user)
        self.stdout.write(f"Created administrator account {username!r}.")

    @staticmethod
    def _create_sample_data():
        FilterRule.objects.get_or_create(
            name="Sample: hold example.com messages",
            defaults={
                "enabled": False,
                "priority": 1000,
                "sender_match": FilterRule.SenderMatch.DOMAIN,
                "sender_value": "example.com",
                "action": FilterRule.Action.HOLD,
            },
        )
        default_template, _ = AcknowledgementTemplate.objects.get_or_create(
            name="Received and printed",
            defaults={
                "body": (
                    'Hello {sender_name}, we received "{subject}" on {date}. '
                    "Regards, {office_name}"
                ),
                "enabled": True,
            },
        )
        AcknowledgementTemplate.objects.get_or_create(
            name="Received for processing",
            defaults={
                "body": (
                    'Hello {sender_name}, we received "{subject}" on {date} '
                    "and it is being processed. Regards, {office_name}"
                ),
                "enabled": True,
            },
        )
        if not AcknowledgementTemplate.objects.filter(is_default=True).exists():
            default_template.is_default = True
            default_template.save(update_fields=["is_default", "updated_at"])
