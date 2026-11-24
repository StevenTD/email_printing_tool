import json
import tempfile
from datetime import date, datetime, timezone
from email.message import EmailMessage
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
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
from .edats import (
    EdatsLookupError,
    fetch_edats_document,
    search_edats_documents,
    subjects_match,
)
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
    def test_imports_manual_gmail_reply_and_advances_sent_cursor(self):
        email = ReceivedEmail.objects.create(
            message_id="<original@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=987,
            imap_uid=501,
            subject="Original message",
        )
        reply = EmailMessage()
        reply["Message-ID"] = "<manual-reply@example.test>"
        reply["In-Reply-To"] = email.message_id
        reply["From"] = "mailbox@example.test"
        reply["To"] = "sender@example.test"
        reply["Subject"] = "Re: Original message"
        reply["Date"] = "Wed, 12 Mar 2025 09:30:00 +0000"
        reply.set_content("I have received your email.")
        reply.add_alternative(
            "<html><body><p><strong>I have received your email.</strong></p></body></html>",
            subtype="html",
        )
        raw_reply = reply.as_bytes()
        unrelated = EmailMessage()
        unrelated["Message-ID"] = "<unrelated@example.test>"
        unrelated["Subject"] = "Not a reply"
        unrelated.set_content("This message should not be downloaded in full.")
        raw_unrelated = unrelated.as_bytes()

        class FakeIMAP:
            def __init__(self):
                self.folder = ""
                self.calls = []

            def login(self, username, password):
                return "OK", [b"logged in"]

            def select(self, folder):
                self.folder = folder
                return "OK", [b"1"]

            def response(self, name):
                return name, [b"987"]

            def uid(self, command, *args):
                self.calls.append((self.folder, command, *args))
                if command == "SEARCH" and self.folder == '"INBOX"':
                    return "OK", [b""]
                if command == "SEARCH":
                    if args[1:] == ("ALL",):
                        return "OK", [b"90 91"]
                    return "OK", [b""]
                if command == "FETCH":
                    if "HEADER.FIELDS" in args[1]:
                        return "OK", [
                            (b"1 (UID 90 BODY[HEADER.FIELDS])", raw_unrelated),
                            (b"2 (UID 91 BODY[HEADER.FIELDS])", raw_reply),
                            b")",
                        ]
                    return "OK", [(b"91 (BODY[])", raw_reply), b")"]
                raise AssertionError(f"Unexpected IMAP command: {command}")

            def logout(self):
                return "BYE", [b"logged out"]

        fake_imap = FakeIMAP()
        configuration = AppConfiguration.objects.create(
            imap_email="mailbox@example.test",
            imap_folder="INBOX",
        )
        status_messages = []
        with patch.object(
            AppConfiguration,
            "get_imap_app_password",
            return_value="app-password",
        ):
            summary = MailFetcher(
                configuration,
                connection_factory=lambda *args, **kwargs: fake_imap,
                status_callback=status_messages.append,
            ).fetch_messages()
            second_summary = MailFetcher(
                configuration,
                connection_factory=lambda *args, **kwargs: fake_imap,
            ).fetch_messages()

        email.refresh_from_db()
        imported_reply = email.acknowledgements.get()
        configuration.refresh_from_db()
        self.assertEqual(summary.replies, 1)
        self.assertEqual(imported_reply.source, Acknowledgement.Source.GMAIL)
        self.assertEqual(imported_reply.status, Acknowledgement.Status.SENT)
        self.assertEqual(imported_reply.sender, "mailbox@example.test")
        self.assertEqual(imported_reply.body.strip(), "I have received your email.")
        self.assertIn("<strong>I have received your email.</strong>", imported_reply.body_html)
        self.assertTrue(email.acknowledged)
        self.assertEqual(configuration.imap_sent_last_uid, 91)
        self.assertEqual(second_summary.replies, 0)
        self.assertEqual(email.acknowledgements.count(), 1)
        self.assertIn(
            ('"[Gmail]/Sent Mail"', "SEARCH", None, "ALL"),
            fake_imap.calls,
        )
        sent_search_index = fake_imap.calls.index(
            ('"[Gmail]/Sent Mail"', "SEARCH", None, "ALL")
        )
        inbox_search_index = fake_imap.calls.index(
            ('"INBOX"', "SEARCH", None, "UNSEEN")
        )
        self.assertLess(sent_search_index, inbox_search_index)
        header_fetches = [
            call
            for call in fake_imap.calls
            if call[1] == "FETCH" and "HEADER.FIELDS" in call[3]
        ]
        body_fetches = [
            call[2]
            for call in fake_imap.calls
            if call[1] == "FETCH" and call[3] == "(BODY.PEEK[])"
        ]
        self.assertEqual(len(header_fetches), 1)
        self.assertEqual(header_fetches[0][2], "90,91")
        self.assertEqual(body_fetches, ["91"])
        self.assertIn(
            "Fetching headers for Gmail Sent messages 1-2 of 2.",
            status_messages,
        )
        self.assertIn(
            ('"[Gmail]/Sent Mail"', "SEARCH", None, "UID", "92:*"),
            fake_imap.calls,
        )

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
            imap_sent_folder="",
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
                ).fetch_messages()

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
            ).fetch_messages()

        self.assertEqual(summary.discovered, 0)
        self.assertIn(
            ("SEARCH", None, "UNSEEN", "SINCE", "12-Mar-2025"),
            fake_imap.calls,
        )

    def test_searches_seen_and_unseen_since_configured_date_when_enabled(self):
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
                fetch_seen_emails=True,
            ).fetch_messages()

        self.assertEqual(summary.discovered, 0)
        self.assertIn(
            ("SEARCH", None, "ALL", "SINCE", "12-Mar-2025"),
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

    def test_inbox_marks_successfully_replied_emails(self):
        replied = ReceivedEmail.objects.create(
            message_id="<replied@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=333,
            imap_uid=502,
            subject="Replied email",
        )
        failed = ReceivedEmail.objects.create(
            message_id="<failed-reply@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=333,
            imap_uid=503,
            subject="Failed reply",
        )
        Acknowledgement.objects.create(
            email=replied,
            recipient="sender@example.test",
            body="Thanks.",
            status=Acknowledgement.Status.SENT,
        )
        Acknowledgement.objects.create(
            email=failed,
            recipient="sender@example.test",
            body="Thanks.",
            status=Acknowledgement.Status.FAILED,
        )

        response = self.client.get(reverse("mails:inbox"))

        self.assertEqual(response.status_code, 200)
        rows = response.content.decode()
        replied_row = rows.split(f'id="email-row-{replied.pk}"', 1)[1].split("</tr>", 1)[0]
        failed_row = rows.split(f'id="email-row-{failed.pk}"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("Replied", replied_row)
        self.assertNotIn("Replied", failed_row)

        replied_filter = self.client.get(
            reverse("mails:inbox"),
            {"reply_status": "replied"},
        )
        self.assertContains(replied_filter, replied.subject)
        self.assertNotContains(replied_filter, failed.subject)
        self.assertNotContains(replied_filter, self.email.subject)

        not_replied_filter = self.client.get(
            reverse("mails:inbox"),
            {"reply_status": "not_replied"},
        )
        self.assertContains(not_replied_filter, failed.subject)
        self.assertContains(not_replied_filter, self.email.subject)
        self.assertNotContains(not_replied_filter, replied.subject)

    def test_email_detail_sanitizes_body_html(self):
        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("<p>Hello</p>", response.context["email_body_srcdoc"])
        self.assertContains(response, 'sandbox=""')
        self.assertIn("script-src 'none'", response.context["email_body_srcdoc"])
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "onclick")
        self.assertNotContains(response, "invalid.test")

    def test_client_email_print_returns_pdf_without_queueing_job(self):
        with patch(
            "mails.views.render_email_to_pdf",
            return_value=b"%PDF-1.7 email",
        ) as render_pdf:
            response = self.client.get(
                reverse("mails:client_print_email", args=[self.email.pk]),
                {"include_replies": "1"},
            )
            payload = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline;", response["Content-Disposition"])
        self.assertIn("allow-modals", response["Content-Security-Policy"])
        self.assertEqual(payload, b"%PDF-1.7 email")
        render_pdf.assert_called_once_with(self.email, include_replies=True)
        self.assertFalse(PrintJob.objects.exists())
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, ReceivedEmail.Status.NEW)

    def test_client_print_attachment_converts_and_returns_inline_pdf(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                attachment = EmailAttachment.objects.create(
                    email=self.email,
                    filename="report.pdf",
                    content_type="application/pdf",
                    printable=True,
                )
                attachment.file.save(
                    "report.pdf",
                    ContentFile(b"%PDF-1.7 attachment"),
                    save=True,
                )
                response = self.client.get(
                    reverse(
                        "mails:client_print_attachment",
                        args=[attachment.pk],
                    )
                )
                payload = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline;", response["Content-Disposition"])
        self.assertEqual(payload, b"%PDF-1.7 attachment")
        self.assertFalse(PrintJob.objects.exists())

    def test_email_detail_offers_client_print_actions(self):
        attachment = EmailAttachment.objects.create(
            email=self.email,
            filename="report.pdf",
            content_type="application/pdf",
            printable=True,
            file="mail/report.pdf",
        )

        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))

        self.assertContains(response, "Print email on this device")
        self.assertContains(response, "Print on this device")
        self.assertContains(
            response,
            reverse("mails:client_print_attachment", args=[attachment.pk]),
        )

    def test_email_detail_preserves_html_css_and_renders_html_in_plain_text(self):
        self.email.body_html = (
            "<html><head><style>.notice { color: #123456; }</style></head>"
            '<body><p class="notice" style="font-weight: bold">Formatted message</p>'
            "<script>unsafe()</script></body></html>"
        )
        self.email.body_text = ""
        self.email.save(update_fields=["body_html", "body_text"])

        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))
        srcdoc = response.context["email_body_srcdoc"]

        self.assertEqual(response.status_code, 200)
        self.assertIn("<style>.notice { color: #123456; }</style>", srcdoc)
        self.assertIn('class="notice"', srcdoc)
        self.assertIn('style="font-weight: bold;"', srcdoc)
        self.assertIn("Formatted message", srcdoc)
        self.assertNotIn("<script>", srcdoc)

        self.email.body_html = ""
        self.email.body_text = "<p><strong>Rendered from text</strong></p>"
        self.email.save(update_fields=["body_html", "body_text"])
        text_html_response = self.client.get(
            reverse("mails:detail", args=[self.email.pk])
        )
        self.assertIn(
            "<p><strong>Rendered from text</strong></p>",
            text_html_response.context["email_body_srcdoc"],
        )

    def test_email_detail_displays_sent_reply_body(self):
        Acknowledgement.objects.create(
            email=self.email,
            source=Acknowledgement.Source.GMAIL,
            recipient="sender@example.test",
            sender="firstbox@example.test",
            subject="Re: Quarterly report",
            body="Earlier reply.",
            sent_at=datetime(2025, 3, 12, 9, tzinfo=timezone.utc),
            status=Acknowledgement.Status.SENT,
        )
        Acknowledgement.objects.create(
            email=self.email,
            source=Acknowledgement.Source.GMAIL,
            recipient="sender@example.test",
            sender="mailbox@example.test",
            subject="Re: Quarterly report",
            body="I have received your email.",
            body_html=(
                '<p onclick="bad()"><strong>I have received your email.</strong></p>'
                "<script>unsafe()</script>"
            ),
            message_id="<reply@example.test>",
            sent_at=datetime(2025, 3, 12, 10, tzinfo=timezone.utc),
            status=Acknowledgement.Status.SENT,
        )

        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))
        response_body = response.content.decode()

        self.assertContains(response, "Conversation")
        self.assertContains(response, "mailbox@example.test")
        self.assertContains(response, "Gmail Sent")
        self.assertContains(
            response,
            "<p><strong>I have received your email.</strong></p>",
            html=True,
        )
        self.assertContains(response, "Reply 1")
        self.assertContains(response, "Reply 2")
        self.assertLess(
            response_body.index("Earlier reply."),
            response_body.index("I have received your email."),
        )
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "onclick")
        self.assertNotContains(response, "<pre")

    def test_print_action_queues_job(self):
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "print"])
        )

        self.email.refresh_from_db()
        job = PrintJob.objects.get(email=self.email)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.email.status, ReceivedEmail.Status.QUEUED)
        self.assertEqual(job.status, PrintJob.Status.QUEUED)
        self.assertFalse(job.print_replies)
        self.assertTrue(AuditLog.objects.filter(action="email_print").exists())

    def test_print_action_can_include_sent_replies(self):
        Acknowledgement.objects.create(
            email=self.email,
            recipient="sender@example.test",
            sender="mailbox@example.test",
            body="Thanks for your message.",
            status=Acknowledgement.Status.SENT,
        )

        detail = self.client.get(reverse("mails:detail", args=[self.email.pk]))
        response = self.client.post(
            reverse("mails:action", args=[self.email.pk, "print"]),
            {"include_replies": "1"},
        )

        self.assertContains(detail, "Include sent replies")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PrintJob.objects.get(email=self.email).print_replies)

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
        self.assertEqual(first.sender, self.configuration.imap_email)
        self.assertEqual(second.sender, self.configuration.imap_email)
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


class EdatsAcknowledgementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="edats-operator",
            password="test-pass",
        )
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        self.user.groups.add(operator_group)
        self.client.force_login(self.user)
        self.email = ReceivedEmail.objects.create(
            message_id="<edats-email@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=913,
            imap_uid=1902,
            sender="Sender Name <sender@example.test>",
            subject="Quarterly report",
        )
        self.template = AcknowledgementTemplate.objects.get(
            is_tracking_template=True,
        )
        self.configuration = AppConfiguration.objects.create(
            imap_email="mailbox@example.test",
            office_name="PENRO Aklan",
        )
        self.edats_document = {
            "tracking_number": "P.AKLAN2026-10184",
            "subject": "Quarterly report",
            "sender": "ICT/Records Unit",
            "tracking_base_url": "https://tracking.penroaklan.com.ph",
            "tracking_url": (
                "https://tracking.penroaklan.com.ph/track/P.AKLAN2026-10184/"
                "?sender_name=ICT%2FRecords+Unit"
            ),
        }

    @override_settings(EDATS_API_TOKEN="test-token")
    def test_fetch_edats_document_uses_token_and_returns_tracking_data(self):
        response = BytesIO(
            json.dumps(
                {
                    "tracking_number": "P.AKLAN2026-10184",
                    "subject": "Quarterly report",
                    "sender": "ICT/Records Unit",
                }
            ).encode("utf-8")
        )
        with patch("mails.edats.urlopen", return_value=response) as urlopen_mock:
            document = fetch_edats_document("P.AKLAN2026-10184")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Token test-token")
        self.assertTrue(
            request.full_url.endswith(
                "/document/tracking/P.AKLAN2026-10184"
            )
        )
        self.assertEqual(document["sender"], "ICT/Records Unit")
        self.assertEqual(
            document["tracking_base_url"],
            "https://tracking.penroaklan.com.ph",
        )
        self.assertEqual(
            document["tracking_url"],
            self.edats_document["tracking_url"],
        )

    def test_edats_lookup_requires_token_configuration(self):
        with override_settings(EDATS_API_TOKEN=""):
            with self.assertRaisesMessage(
                EdatsLookupError,
                "EDATS_API_TOKEN is not configured",
            ):
                fetch_edats_document("P.AKLAN2026-10184")

    @override_settings(
        EDATS_API_TOKEN="test-token",
        EDATS_DOCUMENT_SEARCH_URL=(
            "https://irs.penroaklan.com.ph/api/e_dats/document-paginated"
        ),
    )
    def test_search_uses_five_item_pages_and_formats_receiver_and_date(self):
        response = BytesIO(
            json.dumps(
                {
                    "count": 11,
                    "next": "https://irs.penroaklan.com.ph/api/e_dats/document-paginated?page=3",
                    "previous": "https://irs.penroaklan.com.ph/api/e_dats/document-paginated?page=1",
                    "results": [
                        {
                            "tracking_number": "P.AKLAN2026-10184",
                            "subject": "Quarterly report",
                            "sender": "ICT/Records Unit",
                            "to_department": 7,
                            "to_user": 21,
                            "created_at": "2026-11-24T10:00:00Z",
                        }
                    ],
                }
            ).encode("utf-8")
        )
        with patch("mails.edats.urlopen", return_value=response) as urlopen_mock:
            results = search_edats_documents("Quarterly report", page=2)

        request = urlopen_mock.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["page_size"], ["5"])
        self.assertEqual(query["search"], ["Quarterly report"])
        self.assertEqual(request.get_header("Authorization"), "Token test-token")
        self.assertEqual(results["page_size"], 5)
        self.assertTrue(results["has_next"])
        self.assertTrue(results["has_previous"])
        self.assertEqual(
            results["results"][0]["receiver"],
            "Department ID 7 · User ID 21",
        )
        self.assertEqual(results["results"][0]["sender"], "ICT/Records Unit")
        self.assertIn("Nov 24, 2026", results["results"][0]["created_at"])

    def test_subject_comparison_ignores_case_and_extra_whitespace(self):
        self.assertTrue(subjects_match("  QUARTERLY\n report ", "Quarterly report"))
        self.assertFalse(subjects_match("Quarterly report revised", "Quarterly report"))

    @patch("mails.views.fetch_edats_document")
    def test_subject_mismatch_warns_but_allows_tracking_and_sending(self, lookup):
        lookup.return_value = {
            **self.edats_document,
            "subject": "Different subject",
        }

        url = reverse("mails:edats_acknowledgement", args=[self.email.pk])
        post_data = {
            "tracking_number": self.edats_document["tracking_number"],
            "template": self.template.pk,
        }
        preview = self.client.post(url, {**post_data, "action": "preview"})

        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.context["previewed"])
        self.assertFalse(preview.context["edats_subject_matches"])
        self.assertContains(preview, "alert-warning")
        self.assertContains(preview, "These subjects differ")
        self.email.refresh_from_db()
        self.assertEqual(
            self.email.edats_tracking_number,
            self.edats_document["tracking_number"],
        )

        with patch.object(
            AppConfiguration,
            "get_smtp_app_password",
            return_value="app-password",
        ), patch("mails.acknowledgements.smtplib.SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            sent = self.client.post(
                url,
                {
                    **post_data,
                    "action": "send",
                    "preview_token": preview.context["preview_token"],
                },
            )

        self.assertEqual(sent.status_code, 302)
        self.assertEqual(
            Acknowledgement.objects.get(email=self.email).status,
            Acknowledgement.Status.SENT,
        )

    @patch("mails.views.fetch_edats_document")
    def test_tracking_can_be_verified_without_reply_headers(self, lookup):
        lookup.return_value = self.edats_document
        self.email.message_id = ""
        self.email.save(update_fields=["message_id"])

        response = self.client.post(
            reverse("mails:edats_acknowledgement", args=[self.email.pk]),
            {
                "tracking_number": self.edats_document["tracking_number"],
                "template": self.template.pk,
                "action": "verify",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.email.refresh_from_db()
        self.assertEqual(
            self.email.edats_tracking_number,
            self.edats_document["tracking_number"],
        )

    def test_email_detail_has_edats_acknowledgement_action(self):
        response = self.client.get(reverse("mails:detail", args=[self.email.pk]))

        self.assertContains(response, "Send EDATS acknowledgement")
        self.assertContains(
            response,
            reverse("mails:edats_acknowledgement", args=[self.email.pk]),
        )

    @patch("mails.views.search_edats_documents")
    def test_search_page_displays_results_and_selection_prefills_number(self, search):
        search.return_value = {
            "count": 6,
            "page": 1,
            "page_size": 5,
            "page_count": 2,
            "has_next": True,
            "has_previous": False,
            "results": [
                {
                    "tracking_number": "P.AKLAN2026-10184",
                    "subject": "Quarterly report",
                    "sender": "ICT/Records Unit",
                    "receiver": "Department ID 7",
                    "created_at": "Nov 24, 2026, 06:00 PM",
                }
            ],
        }
        url = reverse("mails:edats_acknowledgement", args=[self.email.pk])

        response = self.client.get(
            url,
            {"search": "Quarterly report", "page": "1"},
        )

        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with("Quarterly report", page=1)
        self.assertContains(response, "ICT/Records Unit")
        self.assertContains(response, "Department ID 7")
        self.assertContains(response, "Nov 24, 2026, 06:00 PM")
        self.assertContains(response, "Next")
        select_url = response.context["search_results"]["results"][0]["select_url"]

        self.assertIn("tracking_number=P.AKLAN2026-10184", select_url)
        selected = self.client.get(
            url,
            {
                "search": "Quarterly report",
                "page": "1",
                "tracking_number": "P.AKLAN2026-10184",
            },
        )

        self.assertEqual(
            selected.context["form"]["tracking_number"].value(),
            "P.AKLAN2026-10184",
        )

    @patch("mails.views.fetch_edats_document")
    def test_tracking_preview_sends_html_reply_after_subject_verification(self, lookup):
        lookup.return_value = self.edats_document
        url = reverse("mails:edats_acknowledgement", args=[self.email.pk])
        post_data = {
            "tracking_number": self.edats_document["tracking_number"],
            "template": self.template.pk,
        }

        preview = self.client.post(
            url,
            {**post_data, "action": "preview"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.context["previewed"])
        self.email.refresh_from_db()
        self.assertEqual(
            self.email.edats_tracking_number,
            self.edats_document["tracking_number"],
        )

        with patch.object(
            AppConfiguration,
            "get_smtp_app_password",
            return_value="app-password",
        ), patch("mails.acknowledgements.smtplib.SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            sent = self.client.post(
                url,
                {
                    **post_data,
                    "action": "send",
                    "preview_token": preview.context["preview_token"],
                },
            )

        self.assertEqual(sent.status_code, 302)
        acknowledgement = Acknowledgement.objects.get(email=self.email)
        self.assertEqual(acknowledgement.status, Acknowledgement.Status.SENT)
        self.assertIn(self.edats_document["tracking_url"], acknowledgement.body_html)
        self.assertIn("Track this document", acknowledgement.body_html)
        sent_message = smtp.send_message.call_args.args[0]
        self.assertEqual(
            sent_message.get_body(preferencelist=("html",)).get_content(),
            acknowledgement.body_html,
        )
