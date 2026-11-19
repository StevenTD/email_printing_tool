import tempfile
from datetime import date
from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AppConfiguration, AuditLog, WorkerLog
from core.access import OPERATOR_GROUP_NAME
from .acknowledgements import (
    DuplicateAcknowledgementError,
    AcknowledgementSendError,
    build_reply_message,
    send_acknowledgement,
)
from .fetcher import MailFetcher, store_message
from .models import Acknowledgement, EmailAttachment, ReceivedEmail
from rules.models import AcknowledgementTemplate
from printing.models import PrintJob, PrintLog


def make_raw_message(
    message_id="<received-001@example.test>",
    attachment_filename="report.pdf",
    cc=None,
    bcc=None,
):
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = "Sender <sender@example.test>"
    message["To"] = "recipient@example.test"
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = "A test message"
    message["Date"] = "Wed, 12 Mar 2025 09:00:00 +0000"
    message.set_content("The email body.")
    message.add_attachment(
        b"%PDF-1.7 sample",
        maintype="application",
        subtype="pdf",
        filename=attachment_filename,
    )
    return message.as_bytes()


class ReceivedEmailUniquenessTests(TestCase):
    def create_email(self, *, message_id, uid):
        return ReceivedEmail.objects.create(
            message_id=message_id,
            imap_folder="INBOX",
            imap_uidvalidity=123,
            imap_uid=uid,
        )

    def test_duplicate_imap_uid_is_rejected(self):
        self.create_email(message_id="<first@example.test>", uid=42)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_email(message_id="<second@example.test>", uid=42)

    def test_duplicate_message_id_in_same_folder_is_rejected(self):
        self.create_email(message_id="<same@example.test>", uid=42)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_email(message_id="<same@example.test>", uid=43)


class MailStorageTests(TestCase):
    def test_stores_email_and_attachments_and_deduplicates(self):
        raw_message = make_raw_message()
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                email, created = store_message(
                    raw_message,
                    folder="INBOX",
                    uid=77,
                    uidvalidity=987,
                    max_attachment_bytes=1024,
                )
                duplicate, duplicate_created = store_message(
                    raw_message,
                    folder="INBOX",
                    uid=77,
                    uidvalidity=987,
                    max_attachment_bytes=1024,
                )

                self.assertTrue(created)
                self.assertFalse(duplicate_created)
                self.assertEqual(email.pk, duplicate.pk)
                self.assertEqual(email.sender, "Sender <sender@example.test>")
                self.assertEqual(email.recipients, ["recipient@example.test"])
                self.assertIn("The email body.", email.body_text)
                self.assertTrue(email.raw_email_file.name.startswith("mail/2025/03/"))
                self.assertIn("received-001_example.test-", email.raw_email_file.name)
                attachment = email.attachments.get()
                self.assertEqual(attachment.filename, "report.pdf")
                self.assertTrue(attachment.printable)
                self.assertEqual(attachment.size_bytes, len(b"%PDF-1.7 sample"))
                self.assertTrue(attachment.file.name.startswith("mail/2025/03/"))
                self.assertEqual(ReceivedEmail.objects.count(), 1)

    def test_long_message_id_and_attachment_name_fit_file_field_paths(self):
        long_message_id = f"<{'a' * 200}@example.test>"
        long_attachment_name = f"{'report' * 60}.pdf"
        raw_message = make_raw_message(long_message_id, long_attachment_name)

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                email, created = store_message(
                    raw_message,
                    folder="INBOX",
                    uid=79,
                    uidvalidity=987,
                    max_attachment_bytes=1024,
                )

        self.assertTrue(created)
        self.assertLessEqual(
            len(email.raw_email_file.name),
            ReceivedEmail._meta.get_field("raw_email_file").max_length,
        )
        attachment = email.attachments.get()
        self.assertEqual(attachment.filename, long_attachment_name)
        self.assertLessEqual(
            len(attachment.file.name),
            EmailAttachment._meta.get_field("file").max_length,
        )

    def test_stores_to_cc_and_bcc_separately(self):
        raw_message = make_raw_message(
            cc="Copy Recipient <cc@example.test>",
            bcc="Hidden Recipient <bcc@example.test>",
        )

        email, created = store_message(
            raw_message,
            folder="INBOX",
            uid=80,
            uidvalidity=987,
            max_attachment_bytes=1024,
        )

        self.assertTrue(created)
        self.assertEqual(email.to_recipients, ["recipient@example.test"])
        self.assertEqual(email.cc_recipients, ["cc@example.test"])
        self.assertEqual(email.bcc_recipients, ["bcc@example.test"])
        self.assertEqual(
            email.recipients,
            ["recipient@example.test", "cc@example.test", "bcc@example.test"],
        )

    def test_oversized_attachment_is_listed_without_saving_its_content(self):
        raw_message = make_raw_message()
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                email, _ = store_message(
                    raw_message,
                    folder="INBOX",
                    uid=78,
                    uidvalidity=987,
                    max_attachment_bytes=2,
                )

                attachment = email.attachments.get()
                self.assertEqual(attachment.size_bytes, len(b"%PDF-1.7 sample"))
                self.assertTrue(attachment.printable)
                self.assertFalse(attachment.file)
                self.assertIn("exceeds", attachment.conversion_error)


class MailFetcherTests(TestCase):
    def test_searches_unseen_peeks_at_content_and_marks_stored_message_seen(self):
        raw_message = make_raw_message()

        class FakeIMAP:
            def __init__(self):
                self.calls = []

            def login(self, username, password):
                self.calls.append(("login", username, password))
                return "OK", [b"logged in"]

            def select(self, folder):
                self.calls.append(("select", folder))
                return "OK", [b"1"]

            def response(self, name):
                return name, [b"987"]

            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b"77"]
                if command == "FETCH":
                    return "OK", [(b"77 (BODY[])", raw_message), b")"]
                if command == "STORE":
                    return "OK", [b"77"]
                raise AssertionError(f"Unexpected IMAP command: {command}")

            def logout(self):
                return "BYE", [b"logged out"]

        fake_imap = FakeIMAP()
        configuration = AppConfiguration(
            imap_email="mailbox@example.test",
            imap_folder="INBOX",
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root), patch.object(
                AppConfiguration,
                "get_imap_app_password",
                return_value="app-password",
            ):
                summary = MailFetcher(
                    configuration,
                    connection_factory=lambda *args, **kwargs: fake_imap,
                ).fetch_unseen()

        self.assertEqual(summary.stored, 1)
        self.assertEqual(summary.errors, 0)
        self.assertIn(("SEARCH", None, "UNSEEN"), fake_imap.calls)
        self.assertIn(("FETCH", "77", "(BODY.PEEK[])"), fake_imap.calls)
        self.assertIn(("STORE", "77", "+FLAGS", r"(\Seen)"), fake_imap.calls)
        stored_email = ReceivedEmail.objects.get()
        self.assertEqual(stored_email.status, ReceivedEmail.Status.HELD)
        self.assertEqual(ReceivedEmail.objects.count(), 1)

    def test_searches_unseen_messages_since_configured_date(self):
        class FakeIMAP:
            def __init__(self):
                self.calls = []

            def login(self, username, password):
                return "OK", [b"logged in"]

            def select(self, folder):
                return "OK", [b"0"]

            def response(self, name):
                return name, [b"987"]

            def uid(self, command, *args):
                self.calls.append((command, *args))
                if command == "SEARCH":
                    return "OK", [b""]
                raise AssertionError(f"Unexpected IMAP command: {command}")

            def logout(self):
                return "BYE", [b"logged out"]

        fake_imap = FakeIMAP()
        configuration = AppConfiguration(
            imap_email="mailbox@example.test",
            imap_folder="INBOX",
        )
        with patch.object(
            AppConfiguration,
            "get_imap_app_password",
            return_value="app-password",
        ):
            summary = MailFetcher(
                configuration,
                connection_factory=lambda *args, **kwargs: fake_imap,
                fetch_since_date=date(2025, 3, 12),
            ).fetch_unseen()

        self.assertEqual(summary.discovered, 0)
        self.assertIn(
            ("SEARCH", None, "UNSEEN", "SINCE", "12-Mar-2025"),
            fake_imap.calls,
        )


class MailInboxViewTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="operator",
            password="test-pass",
        )
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        self.client.force_login(user)
        self.email = ReceivedEmail.objects.create(
            message_id="<view-test@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=333,
            imap_uid=501,
            sender="Sender <sender@example.test>",
            recipients=["operator@example.test"],
            subject="Quarterly report",
            body_text="See the report.",
            body_html='<script>unsafe()</script><p onclick="bad()">Hello</p>',
        )

    def test_inbox_filters_by_status_and_search(self):
        held = ReceivedEmail.objects.create(
            message_id="<held@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=333,
            imap_uid=502,
            sender="held@example.test",
            subject="Held note",
            status=ReceivedEmail.Status.HELD,
        )

        response = self.client.get(
            reverse("mails:inbox"),
            {"status": "held", "q": "Held"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, held.subject)
        self.assertNotContains(response, self.email.subject)

    def test_email_detail_sanitizes_body_html(self):
        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<p>Hello</p>", html=True)
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "onclick")
        self.assertNotContains(response, "invalid.test")

    def test_print_action_queues_job(self):
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "print"])
        )

        self.email.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.email.status, ReceivedEmail.Status.QUEUED)
        self.assertEqual(
            PrintJob.objects.get(email=self.email).status,
            PrintJob.Status.QUEUED,
        )
        self.assertTrue(AuditLog.objects.filter(action="email_print").exists())

    def test_operator_can_queue_printable_attachments_without_email_body(self):
        self.email.status = ReceivedEmail.Status.PRINTED
        self.email.save(update_fields=["status"])
        attachment = EmailAttachment.objects.create(
            email=self.email,
            filename="sample.pdf",
            content_type="application/pdf",
            printable=True,
            file="mail/sample.pdf",
        )

        detail = self.client.get(reverse("mails:detail", args=[self.email.pk]))
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "print_attachments"])
        )

        job = PrintJob.objects.get(email=self.email)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Print attachments only")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(job.print_email_body)
        self.assertTrue(job.print_attachments)
        self.assertTrue(job.explicit_reprint)
        self.assertFalse(job.automatic)
        self.assertTrue(AuditLog.objects.filter(action="email_print_attachments").exists())
        self.assertEqual(attachment.email_id, self.email.pk)

    def test_print_attachments_action_rejects_emails_without_printable_files(self):
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "print_attachments"])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PrintJob.objects.exists())

    def test_mark_printed_records_manual_history_without_printing(self):
        admin = get_user_model().objects.create_user(
            username="mail-admin",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "mark_printed"])
        )

        self.email.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.email.status, ReceivedEmail.Status.PRINTED)
        self.assertEqual(
            PrintLog.objects.get(email=self.email).result,
            PrintLog.Result.MANUAL,
        )

    def test_operator_cannot_change_email_status_without_printing(self):
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "mark_printed"])
        )

        self.assertEqual(response.status_code, 403)
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, ReceivedEmail.Status.NEW)

    def test_attachment_download_requires_login_and_returns_file(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                attachment = EmailAttachment.objects.create(
                    email=self.email,
                    filename="evidence.txt",
                    content_type="text/plain",
                    size_bytes=7,
                )
                attachment.file.save(
                    "evidence.txt",
                    ContentFile(b"evidence"),
                    save=True,
                )

                response = self.client.get(
                    reverse("mails:download_attachment", args=[attachment.pk])
                )
                payload = b"".join(response.streaming_content)

                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertEqual(payload, b"evidence")


class AcknowledgementTests(TestCase):
    def setUp(self):
        self.email = ReceivedEmail.objects.create(
            message_id="<received-ack@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=912,
            imap_uid=1901,
            sender="Sender Name <sender@example.test>",
            subject="Quarterly report",
            body_text="Report attached.",
        )
        self.template = AcknowledgementTemplate.objects.create(
            name="Received",
            body="Hello {sender_name}, received {subject} for {office_name}.",
            is_default=True,
        )
        self.configuration = AppConfiguration.objects.create(
            imap_email="mailbox@example.test",
            office_name="Main Office",
        )

    def test_reply_uses_original_thread_headers_and_renders_variables(self):
        raw_message = EmailMessage()
        raw_message["Message-ID"] = self.email.message_id
        raw_message["References"] = "<root@example.test>"
        raw_message["From"] = self.email.sender
        raw_message["Subject"] = self.email.subject
        self.email.raw_email_file.save(
            "incoming.eml",
            ContentFile(raw_message.as_bytes()),
            save=True,
        )

        message, recipient, body = build_reply_message(
            self.email,
            self.template,
            self.configuration,
        )

        self.assertEqual(recipient, "sender@example.test")
        self.assertEqual(message["In-Reply-To"], self.email.message_id)
        self.assertEqual(
            message["References"],
            "<root@example.test> <received-ack@example.test>",
        )
        self.assertIn("Hello Sender Name, received Quarterly report for Main Office.", body)

    def test_duplicate_acknowledgement_requires_explicit_confirmation(self):
        user = get_user_model().objects.create_user(
            username="operator",
            password="test-pass",
        )
        with patch.object(
            AppConfiguration,
            "get_smtp_app_password",
            return_value="app-password",
        ), patch("mails.acknowledgements.smtplib.SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            first = send_acknowledgement(self.email, self.template, user=user)
            with self.assertRaises(DuplicateAcknowledgementError):
                send_acknowledgement(self.email, self.template, user=user)
            second = send_acknowledgement(
                self.email,
                self.template,
                user=user,
                confirm_duplicate=True,
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Acknowledgement.Status.SENT)
        self.assertEqual(second.status, Acknowledgement.Status.SENT)
        self.assertEqual(smtp.send_message.call_count, 2)
        self.email.refresh_from_db()
        self.assertTrue(self.email.acknowledged)
        self.assertIsNotNone(self.email.acknowledged_at)

    def test_smtp_failure_is_logged_and_preserved_in_acknowledgement_history(self):
        with patch.object(
            AppConfiguration,
            "get_smtp_app_password",
            return_value="app-password",
        ), patch(
            "mails.acknowledgements.smtplib.SMTP_SSL"
        ) as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.side_effect = OSError("SMTP unavailable")
            with self.assertRaises(AcknowledgementSendError):
                send_acknowledgement(self.email, self.template)

        acknowledgement = Acknowledgement.objects.get(email=self.email)
        self.assertEqual(acknowledgement.status, Acknowledgement.Status.FAILED)
        self.assertIn("SMTP unavailable", acknowledgement.error_message)
        self.assertTrue(
            WorkerLog.objects.filter(component="smtp", email=self.email).exists()
        )

    def test_compose_view_requires_a_preview_and_duplicate_confirmation(self):
        user = get_user_model().objects.create_user(
            username="operator",
            password="test-pass",
        )
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        self.client.force_login(user)
        with patch.object(
            AppConfiguration,
            "get_smtp_app_password",
            return_value="app-password",
        ), patch("mails.acknowledgements.smtplib.SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            preview = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {"template": self.template.pk, "action": "preview"},
            )
            token = preview.context["preview_token"]
            self.assertEqual(preview.context["preview_recipient"], "sender@example.test")
            self.assertEqual(preview.context["preview_subject"], "Re: Quarterly report")
            rejected_send = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {"template": self.template.pk, "action": "send"},
            )
            self.assertEqual(rejected_send.status_code, 200)
            self.assertFalse(Acknowledgement.objects.exists())

            first_send = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {
                    "template": self.template.pk,
                    "action": "send",
                    "preview_token": token,
                },
            )
            self.assertEqual(first_send.status_code, 302)

            second_preview = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {"template": self.template.pk, "action": "preview"},
            )
            second_token = second_preview.context["preview_token"]
            needs_confirmation = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {
                    "template": self.template.pk,
                    "action": "send",
                    "preview_token": second_token,
                },
            )
            self.assertTrue(
                needs_confirmation.context["duplicate_confirmation_required"]
            )
            second_send = self.client.post(
                reverse("mails:acknowledgement", args=[self.email.pk]),
                {
                    "template": self.template.pk,
                    "action": "send",
                    "preview_token": second_token,
                    "confirm_duplicate": "on",
                },
            )

        self.assertEqual(second_send.status_code, 302)
        self.assertEqual(Acknowledgement.objects.filter(status="sent").count(), 2)
