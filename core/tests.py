from datetime import date
from io import StringIO
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core.access import ADMIN_GROUP_NAME, OPERATOR_GROUP_NAME
from core.management.commands.run_worker import _configured_fetch_since_date
from core.models import AuditLog
from mails.models import ReceivedEmail
from .models import AppConfiguration
from rules.models import AcknowledgementTemplate, FilterRule


class EncryptedCredentialTests(SimpleTestCase):
    def test_app_password_is_encrypted_before_storage(self):
        key = Fernet.generate_key().decode("ascii")
        configuration = AppConfiguration()

        with patch("core.secrets.config", return_value=key):
            configuration.set_imap_app_password("gmail-app-password")
            encrypted_value = configuration.imap_app_password_encrypted

            self.assertNotEqual(encrypted_value, "gmail-app-password")
            self.assertEqual(configuration.get_imap_app_password(), "gmail-app-password")


class FetchSinceConfigurationTests(SimpleTestCase):
    @patch("core.management.commands.run_worker.config", return_value="2025-03-12")
    def test_parses_configured_cutoff_date(self, _config):
        self.assertEqual(_configured_fetch_since_date(), date(2025, 3, 12))

    @patch("core.management.commands.run_worker.config", return_value="")
    def test_blank_cutoff_date_disables_date_filter(self, _config):
        self.assertIsNone(_configured_fetch_since_date())

    @patch("core.management.commands.run_worker.config", return_value="03/12/2025")
    def test_rejects_non_iso_cutoff_date(self, _config):
        with self.assertRaises(CommandError):
            _configured_fetch_since_date()


class WorkerOutputTests(TestCase):
    def test_worker_flushes_startup_and_gmail_poll_status(self):
        AppConfiguration.objects.create(
            imap_email="mailbox@example.test",
            imap_app_password_encrypted="configured",
        )
        fetcher_startup_output = []

        class FlushTrackingStream(StringIO):
            def __init__(self):
                super().__init__()
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1
                super().flush()

        output = FlushTrackingStream()

        class FakeFetcher:
            def __init__(self, _configuration, **kwargs):
                self.status_callback = kwargs["status_callback"]
                fetcher_startup_output.append(output.getvalue())

            def fetch_messages(self):
                self.status_callback("Connecting to Gmail IMAP (imap.gmail.com:993).")
                self.status_callback("Gmail IMAP login succeeded.")
                return SimpleNamespace(
                    stored=0,
                    duplicates=0,
                    replies=0,
                    errors=0,
                )

        with patch(
            "core.management.commands.run_worker.MailFetcher",
            FakeFetcher,
        ), patch(
            "core.management.commands.run_worker.process_next_job",
            return_value=None,
        ):
            call_command("run_worker", "--once", stdout=output)

        self.assertIn("Starting MailPrint worker...", output.getvalue())
        self.assertIn("Worker is running.", output.getvalue())
        self.assertIn("Connecting to Gmail IMAP", output.getvalue())
        self.assertIn("Gmail IMAP login succeeded.", output.getvalue())
        self.assertIn("Gmail poll complete:", output.getvalue())
        self.assertIn("MailPrint worker stopped.", output.getvalue())
        self.assertGreater(output.flush_count, 0)
        self.assertIn("Starting MailPrint worker...", fetcher_startup_output[0])


class DashboardViewTests(TestCase):
    def test_dashboard_requires_login(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_dashboard_shows_counts_and_polls_statistics(self):
        user = get_user_model().objects.create_user(username="operator", password="test-pass")
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        self.client.force_login(user)
        AppConfiguration.objects.create(auto_print_enabled=True)
        ReceivedEmail.objects.create(
            imap_folder="INBOX",
            imap_uidvalidity=101,
            imap_uid=1,
            status=ReceivedEmail.Status.NEW,
        )
        ReceivedEmail.objects.create(
            imap_folder="INBOX",
            imap_uidvalidity=101,
            imap_uid=2,
            status=ReceivedEmail.Status.HELD,
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Auto-print")
        self.assertContains(response, "htmx.org")
        self.assertEqual(response.context["new_count"], 1)
        self.assertEqual(response.context["held_count"], 1)
        self.assertTrue(response.context["auto_print_enabled"])

    def test_authenticated_user_without_a_role_is_denied(self):
        user = get_user_model().objects.create_user(
            username="unassigned",
            password="test-pass",
        )
        self.client.force_login(user)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 403)


class AuthenticationSecurityTests(TestCase):
    def test_successful_login_and_logout_are_audited(self):
        user = get_user_model().objects.create_user(
            username="operator",
            password="valid-password-123",
        )
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        client = Client()

        response = client.post(
            "/accounts/login/",
            {"username": "operator", "password": "valid-password-123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action="login_succeeded", user=user).exists())

        response = client.post("/accounts/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action="logout", user=user).exists())

    def test_login_is_rate_limited_after_configured_failures(self):
        client = Client()
        for _ in range(4):
            response = client.post(
                "/accounts/login/",
                {"username": "blocked-user", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 200)

        locked_response = client.post(
            "/accounts/login/",
            {"username": "blocked-user", "password": "wrong"},
        )

        self.assertEqual(locked_response.status_code, 429)


class SeedMailPrintCommandTests(TestCase):
    def test_seed_command_is_idempotent_and_creates_roles_samples(self):
        call_command("seed_mailprint", "--skip-admin")
        call_command("seed_mailprint", "--skip-admin")

        self.assertTrue(Group.objects.filter(name=ADMIN_GROUP_NAME).exists())
        self.assertTrue(Group.objects.filter(name=OPERATOR_GROUP_NAME).exists())
        self.assertEqual(FilterRule.objects.filter(name__startswith="Sample:").count(), 1)
        self.assertEqual(AcknowledgementTemplate.objects.count(), 2)
        self.assertEqual(
            AcknowledgementTemplate.objects.filter(is_default=True).count(),
            1,
        )


class UserDepartmentAdminTests(TestCase):
    def test_department_can_be_set_from_user_admin(self):
        admin_user = get_user_model().objects.create_superuser(
            username="admin",
            password="test-pass",
            email="admin@example.test",
        )
        user = get_user_model().objects.create_user(
            username="department-user",
            password="test-pass",
        )
        self.client.force_login(admin_user)

        response = self.client.get(
            reverse("admin:auth_user_change", args=[user.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Department")
