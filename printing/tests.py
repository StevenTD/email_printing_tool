from contextlib import nullcontext
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AppConfiguration, AuditLog
from mails.models import Acknowledgement, EmailAttachment, ReceivedEmail

from .conversion import build_email_footer_html, build_email_html
from .forms import PrinterProfileForm
from .models import PrintJob, PrintLog, PrinterProfile
from .pipeline import (
    PrintPipelineError,
    _send_pdf_to_printer,
    _print_job_files,
    create_print_job,
    process_next_job,
)
from .printers import (
    PrinterManagementError,
    cancel_printer_job,
    list_installed_printers,
    list_printer_jobs,
    wait_for_printer_queue_empty,
)


class FakeWin32Error(Exception):
    pass


class FakePyWinTypes:
    error = FakeWin32Error


class FakeWin32Print:
    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4
    PRINTER_STATUS_ERROR = 2
    PRINTER_STATUS_OFFLINE = 128
    PRINTER_STATUS_PRINTING = 1024
    JOB_CONTROL_CANCEL = 3

    @classmethod
    def EnumPrinters(cls, flags, name, level):
        return [
            {"pPrinterName": "Desk Printer", "Status": 0, "cJobs": 1},
            {
                "pPrinterName": "Offline Printer",
                "Status": cls.PRINTER_STATUS_OFFLINE,
                "cJobs": 0,
            },
            {
                "pPrinterName": "Broken Printer",
                "Status": cls.PRINTER_STATUS_ERROR,
                "cJobs": 0,
            },
        ]

    @staticmethod
    def GetDefaultPrinter():
        return "Desk Printer"

    @staticmethod
    def OpenPrinter(printer_name):
        return printer_name

    @staticmethod
    def EnumJobs(handle, first, count, level):
        return [
            {
                "JobId": 15,
                "pDocument": "Quarterly report.pdf",
                "pUserName": "operator",
                "pStatus": "Printing",
                "PagesPrinted": 1,
                "TotalPages": 4,
            }
        ]

    @staticmethod
    def ClosePrinter(handle):
        return None

    @classmethod
    def SetJob(cls, handle, job_id, level, info, command):
        cls.cancelled = (handle, job_id, level, info, command)


class PrinterApiTests(TestCase):
    def test_inventory_includes_status_default_profile_and_offline_profiles(self):
        profile = PrinterProfile.objects.create(
            printer_name="Desk Printer",
            alias="Front Desk",
            dedicated_purpose="Orders",
        )
        offline_profile = PrinterProfile.objects.create(
            printer_name="Not Connected",
            is_default=True,
        )
        with patch(
            "printing.printers._win32_modules",
            return_value=(FakeWin32Print, FakePyWinTypes),
        ):
            printers = list_installed_printers()

        by_name = {printer["name"]: printer for printer in printers}
        self.assertEqual(by_name["Desk Printer"]["status"], "ready")
        self.assertTrue(by_name["Desk Printer"]["is_system_default"])
        self.assertEqual(by_name["Desk Printer"]["alias"], "Front Desk")
        self.assertEqual(by_name["Desk Printer"]["dedicated_purpose"], "Orders")
        self.assertEqual(by_name["Offline Printer"]["status"], "offline")
        self.assertEqual(by_name["Broken Printer"]["status"], "error")
        self.assertFalse(by_name["Not Connected"]["installed"])
        self.assertEqual(by_name["Not Connected"]["profile"], offline_profile)

    def test_queue_enumeration_and_cancellation_use_windows_job_ids(self):
        with patch(
            "printing.printers._win32_modules",
            return_value=(FakeWin32Print, FakePyWinTypes),
        ):
            jobs = list_printer_jobs("Desk Printer")
            cancel_printer_job("Desk Printer", 15)

        self.assertEqual(jobs[0]["job_id"], 15)
        self.assertEqual(jobs[0]["document"], "Quarterly report.pdf")
        self.assertEqual(
            FakeWin32Print.cancelled,
            ("Desk Printer", 15, 0, None, FakeWin32Print.JOB_CONTROL_CANCEL),
        )

    def test_queue_cancellation_rejects_nonpositive_job_id(self):
        with self.assertRaises(PrinterManagementError):
            cancel_printer_job("Desk Printer", 0)


class PrinterProfileFormTests(TestCase):
    def test_profile_saves_friendly_alias_settings_and_default(self):
        form = PrinterProfileForm(
            data={
                "printer_name": "Desk Printer",
                "alias": "Front Desk",
                "dedicated_purpose": "Orders",
                "is_default": "on",
                "paper_size": "A5",
                "duplex": "on",
                "color_mode": "mono",
            },
            printer_names=["Desk Printer"],
        )

        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()

        self.assertEqual(profile.printer_name, "Desk Printer")
        self.assertEqual(profile.alias, "Front Desk")
        self.assertEqual(profile.paper_size, "A5")
        self.assertTrue(profile.duplex)
        self.assertEqual(profile.color_mode, PrinterProfile.ColorMode.MONO)
        self.assertTrue(profile.is_default)


class PrinterManagementViewTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="admin",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(user)

    def test_printer_page_renders_inventory_and_queue(self):
        inventory = [
            {
                "name": "Desk Printer",
                "alias": "Front Desk",
                "is_default": True,
                "is_system_default": True,
                "dedicated_purpose": "Orders",
                "status": "ready",
                "status_detail": "Ready",
                "job_count": 1,
                "profile": None,
                "installed": True,
            }
        ]
        with patch(
            "printing.views.list_installed_printers",
            return_value=inventory,
        ), patch(
            "printing.views.list_printer_jobs",
            return_value=[
                {
                    "job_id": 15,
                    "document": "Quarterly report.pdf",
                    "user": "operator",
                    "status": "Printing",
                    "pages_printed": 1,
                    "total_pages": 4,
                }
            ],
        ):
            response = self.client.get(reverse("printing:printers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Front Desk")
        self.assertContains(response, "Quarterly report.pdf")
        self.assertContains(response, "Print test page")

    def test_dry_run_test_page_saves_pdf_without_printing(self):
        AppConfiguration.objects.create(dry_run_enabled=True)
        with tempfile.TemporaryDirectory() as output_directory, patch(
            "printing.views.list_installed_printers",
            return_value=[
                {
                    "name": "Desk Printer",
                    "installed": True,
                }
            ],
        ), patch(
            "printing.views.render_email_to_pdf",
            return_value=b"%PDF-1.7 test",
        ), patch(
            "printing.views.config",
            return_value=output_directory,
        ), patch(
            "printing.views._send_pdf_to_printer",
            side_effect=AssertionError("Dry-run must not print."),
        ):
            response = self.client.post(
                reverse("printing:test_page"),
                {"printer_name": "Desk Printer"},
            )
            generated_files = list(
                Path(output_directory).glob("test_pages/*.pdf")
            )
            generated_payloads = [path.read_bytes() for path in generated_files]

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(generated_payloads), 1)
        self.assertEqual(generated_payloads[0], b"%PDF-1.7 test")
        self.assertTrue(AuditLog.objects.filter(action="printer_test_page").exists())

    def test_sumatra_path_is_saved_and_audited(self):
        response = self.client.post(
            reverse("printing:sumatra_settings"),
            {"sumatra_pdf_path": r"C:\Tools\SumatraPDF.exe"},
        )

        configuration = AppConfiguration.objects.get(key=1)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(configuration.sumatra_pdf_path, r"C:\Tools\SumatraPDF.exe")
        self.assertTrue(AuditLog.objects.filter(action="sumatra_path_updated").exists())

    def test_cancel_queue_job_uses_selected_windows_printer(self):
        with patch(
            "printing.views.list_installed_printers",
            return_value=[{"name": "Desk Printer", "installed": True}],
        ), patch("printing.views.cancel_printer_job") as cancel:
            response = self.client.post(
                reverse("printing:job_cancel"),
                {"printer_name": "Desk Printer", "job_id": "15"},
            )

        self.assertEqual(response.status_code, 302)
        cancel.assert_called_once_with("Desk Printer", 15)
        self.assertTrue(AuditLog.objects.filter(action="printer_job_cancelled").exists())


class PrintPipelineTests(TestCase):
    def create_email(self):
        return ReceivedEmail.objects.create(
            message_id="<print-test@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=456,
            imap_uid=900,
            sender="sender@example.test",
            subject="A printable message",
            body_text="Printable body",
            body_html="<p>Printable body</p>",
        )

    def test_dry_run_writes_pdfs_and_never_calls_sumatra(self):
        email = self.create_email()
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                attachment = EmailAttachment.objects.create(
                    email=email,
                    filename="report.pdf",
                    content_type="application/pdf",
                    printable=True,
                )
                attachment.file.save("report.pdf", ContentFile(b"%PDF-1.7"), save=True)
                job = create_print_job(email, explicit_reprint=False)
                job.dry_run = True
                job.save(update_fields=["dry_run"])

                with tempfile.TemporaryDirectory() as output_root, patch(
                    "printing.pipeline.config",
                    return_value=output_root,
                ), patch(
                    "printing.pipeline.render_email_to_pdf",
                    return_value=b"%PDF-1.7 body",
                ), patch(
                    "printing.pipeline.subprocess.run",
                    side_effect=AssertionError("Dry-run must not invoke a printer."),
                ):
                    result = process_next_job()
                    output_files = list(Path(output_root).glob("job-*/*.pdf"))

                self.assertEqual(result.status, PrintJob.Status.DRY_RUN)
                email.refresh_from_db()
                self.assertEqual(email.status, ReceivedEmail.Status.NEW)
                self.assertEqual(len(output_files), 2)
                self.assertEqual(
                    PrintLog.objects.filter(result=PrintLog.Result.DRY_RUN).count(),
                    2,
                )

    def test_waits_for_each_print_to_leave_queue_before_sending_next_file(self):
        email = self.create_email()
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                attachment = EmailAttachment.objects.create(
                    email=email,
                    filename="report.pdf",
                    content_type="application/pdf",
                    printable=True,
                )
                attachment.file.save(
                    "report.pdf",
                    ContentFile(b"%PDF-1.7 attachment"),
                    save=True,
                )
                job = PrintJob.objects.create(
                    email=email,
                    print_email_body=True,
                    print_replies=True,
                    print_attachments=True,
                )
                events = []
                with patch(
                    "printing.pipeline.render_email_to_pdf",
                    return_value=b"%PDF-1.7 body",
                ) as render_email, patch(
                    "printing.pipeline._send_pdf_to_printer",
                    side_effect=lambda path, *args, **kwargs: events.append(
                        ("send", Path(path).name)
                    ),
                ), patch(
                    "printing.pipeline.wait_for_printer_queue_empty",
                    side_effect=lambda printer: events.append(("wait", printer)),
                ):
                    completed = _print_job_files(
                        job,
                        email,
                        AppConfiguration(key=1),
                        "Test printer",
                        False,
                    )

        self.assertEqual(completed, 2)
        render_email.assert_called_once_with(email, include_replies=True)
        self.assertEqual(
            [event[0] for event in events],
            ["send", "wait", "send"],
        )
        self.assertEqual(events[1], ("wait", "Test printer"))

    def test_wait_for_printer_queue_checks_until_jobs_are_gone(self):
        class FakeWin32Print:
            calls = 0

            @staticmethod
            def OpenPrinter(printer_name):
                return printer_name

            @classmethod
            def GetPrinter(cls, handle, level):
                cls.calls += 1
                return {"cJobs": 1 if cls.calls == 1 else 0}

            @staticmethod
            def EnumJobs(handle, first, count, level):
                return [{"Status": 0, "pStatus": "Printing"}]

            @staticmethod
            def ClosePrinter(handle):
                return None

        class FakePyWinTypes:
            error = FakeWin32Error

        with patch(
            "printing.printers._win32_modules",
            return_value=(FakeWin32Print, FakePyWinTypes),
        ), patch("printing.printers.time.sleep"):
            wait_for_printer_queue_empty(
                "Test printer",
                timeout_seconds=5,
                poll_interval_seconds=0.01,
            )

        self.assertEqual(FakeWin32Print.calls, 2)

    def test_successful_print_log_blocks_a_non_explicit_reprint(self):
        email = self.create_email()
        first_job = PrintJob.objects.create(
            email=email,
            status=PrintJob.Status.PRINTED,
            explicit_reprint=False,
        )
        PrintLog.objects.create(
            email=email,
            job=first_job,
            filename="body.pdf",
            result=PrintLog.Result.SUCCESS,
        )
        job = PrintJob.objects.create(email=email, print_email_body=True)

        result = process_next_job()

        job.refresh_from_db()
        self.assertEqual(result.pk, job.pk)
        self.assertEqual(job.status, PrintJob.Status.FAILED)
        self.assertIn("explicit reprint", job.error_message)
        email.refresh_from_db()
        self.assertEqual(email.status, ReceivedEmail.Status.PRINTED)

    def test_printed_email_requires_explicit_reprint_when_enqueuing(self):
        email = self.create_email()
        email.status = ReceivedEmail.Status.PRINTED
        email.save(update_fields=["status"])

        with self.assertRaises(PrintPipelineError):
            create_print_job(email)

    def test_auto_acknowledgement_runs_after_physical_print_only_when_enabled(self):
        email = self.create_email()
        configuration = AppConfiguration.objects.create(
            auto_acknowledge_after_print=True,
        )
        job = create_print_job(email)

        with patch(
            "printing.pipeline._resolve_printer",
            return_value="Test printer",
        ), patch(
            "printing.pipeline.printer_mutex",
            return_value=nullcontext(),
        ), patch(
            "printing.pipeline._print_job_files",
            return_value=1,
        ), patch(
            "mails.acknowledgements.send_auto_acknowledgement"
        ) as send_auto_acknowledgement:
            result = process_next_job()

        configuration.refresh_from_db()
        self.assertEqual(result.status, PrintJob.Status.PRINTED)
        send_auto_acknowledgement.assert_called_once()

    def test_auto_acknowledgement_defaults_off(self):
        email = self.create_email()
        job = create_print_job(email)

        with patch(
            "printing.pipeline._resolve_printer",
            return_value="Test printer",
        ), patch(
            "printing.pipeline.printer_mutex",
            return_value=nullcontext(),
        ), patch(
            "printing.pipeline._print_job_files",
            return_value=1,
        ), patch(
            "mails.acknowledgements.send_auto_acknowledgement"
        ) as send_auto_acknowledgement:
            result = process_next_job()

        self.assertEqual(result.status, PrintJob.Status.PRINTED)
        send_auto_acknowledgement.assert_not_called()

    def test_dry_run_never_sends_auto_acknowledgement(self):
        email = self.create_email()
        AppConfiguration.objects.create(
            auto_acknowledge_after_print=True,
            dry_run_enabled=True,
        )
        job = create_print_job(email)

        with tempfile.TemporaryDirectory() as output_directory, patch(
            "printing.pipeline.config",
            return_value=output_directory,
        ), patch(
            "printing.pipeline._print_job_files",
            return_value=1,
        ), patch(
            "mails.acknowledgements.send_auto_acknowledgement"
        ) as send_auto_acknowledgement:
            result = process_next_job()

        self.assertEqual(result.status, PrintJob.Status.DRY_RUN)
        send_auto_acknowledgement.assert_not_called()

    def test_sumatra_receives_safe_argument_list_and_copy_setting(self):
        profile = PrinterProfile(
            printer_name="Front Desk Printer",
            paper_size="Letter",
            duplex=True,
            color_mode=PrinterProfile.ColorMode.MONO,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "mail body.pdf"
            pdf_path.write_bytes(b"%PDF")
            with patch(
                "printing.pipeline.shutil.which",
                return_value="C:\\SumatraPDF.exe",
            ), patch(
                "printing.pipeline.subprocess.run",
                return_value=CompletedProcess([], 0, stdout="", stderr=""),
            ) as run:
                _send_pdf_to_printer(
                    pdf_path,
                    "Front Desk Printer",
                    3,
                    "SumatraPDF.exe",
                    profile=profile,
                )

        command = run.call_args.args[0]
        self.assertEqual(command[1:4], ["-print-to", "Front Desk Printer", "-silent"])
        self.assertIn("-print-settings", command)
        settings_index = command.index("-print-settings") + 1
        self.assertEqual(
            command[settings_index],
            "3x,paper=Letter,duplex,monochrome,fit",
        )
        self.assertEqual(command[-1], str(pdf_path))
        self.assertFalse(run.call_args.kwargs.get("shell", False))


class EmailHtmlConversionTests(TestCase):
    def test_print_layout_includes_email_metadata_and_tool_footer(self):
        email = ReceivedEmail.objects.create(
            message_id="<print-layout@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=987,
            imap_uid=902,
            sender="Sender <sender@example.test>",
            recipients=["to@example.test", "cc@example.test", "bcc@example.test"],
            to_recipients=["to@example.test"],
            cc_recipients=["cc@example.test"],
            bcc_recipients=["bcc@example.test"],
            subject="Quarterly report",
            body_text="Report body",
            date_received=timezone.now(),
        )
        EmailAttachment.objects.create(
            email=email,
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=1234,
            printable=True,
        )

        html = build_email_html(email)

        self.assertIn("MAILPRINT", html)
        self.assertIn("Quarterly report", html)
        self.assertIn("Email date", html)
        self.assertIn("Received by MailPrint", html)
        self.assertIn("sender@example.test", html)
        self.assertIn("to@example.test", html)
        self.assertIn("cc@example.test", html)
        self.assertIn("bcc@example.test", html)
        self.assertIn("report.pdf (1,234 bytes; printable)", html)
        self.assertIn("Print copy generated:", html)
        self.assertIn("Report body", html)
        self.assertIn(
            "@page { size: A4; margin: 10mm 12mm 16mm 12mm; }",
            html,
        )
        self.assertIn("font-size: 10pt; color: #17212b;", html)
        self.assertIn(
            ".metadata { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));",
            html,
        )
        self.assertIn("class=\"metadata-item metadata-item-wide\"", html)

        footer = build_email_footer_html(email)
        self.assertIn("Source email: Sender &lt;sender@example.test&gt;", footer)
        self.assertIn("printed using MailPrint Email Monitoring Tool", footer)
        self.assertIn('class="pageNumber"', footer)
        self.assertIn('class="totalPages"', footer)

    def test_replies_are_included_only_when_requested_and_are_sanitized(self):
        email = ReceivedEmail.objects.create(
            imap_folder="INBOX",
            imap_uidvalidity=987,
            imap_uid=903,
            subject="Quarterly report",
            body_text="Original body",
        )
        Acknowledgement.objects.create(
            email=email,
            recipient="sender@example.test",
            sender="mailbox@example.test",
            subject="Re: Quarterly report",
            body="Thanks for your message.",
            body_html='<p onclick="bad()">Thanks for your message.</p><script>unsafe()</script>',
            status=Acknowledgement.Status.SENT,
        )
        Acknowledgement.objects.create(
            email=email,
            recipient="sender@example.test",
            body="Failed message should not print.",
            status=Acknowledgement.Status.FAILED,
        )

        without_replies = build_email_html(email)
        with_replies = build_email_html(email, include_replies=True)

        self.assertNotIn("Thanks for your message.", without_replies)
        self.assertIn("Replies (1)", with_replies)
        self.assertIn("From:</strong> mailbox@example.test", with_replies)
        self.assertIn("To:</strong> sender@example.test", with_replies)
        self.assertIn("<p>Thanks for your message.</p>", with_replies)
        self.assertNotIn("<script>", with_replies)
        self.assertNotIn("onclick", with_replies)
        self.assertNotIn("Failed message should not print.", with_replies)

    def test_html_is_sanitized_before_rendering(self):
        email = ReceivedEmail(
            imap_uid=1,
            body_html=(
                '<script>print("unsafe")</script>'
                '<p onclick="bad()">Hello</p><img src="https://invalid.test/image.png">'
            ),
        )

        html = build_email_html(email)

        self.assertNotIn("<script>", html)
        self.assertNotIn("onclick", html)
        self.assertIn("<p>Hello</p>", html)
        self.assertIn("Content-Security-Policy", html)

    def test_html_css_formatting_is_preserved_and_active_markup_is_removed(self):
        email = ReceivedEmail(
            imap_uid=2,
            body_html=(
                "<html><head><style>.notice { color: #123456; }</style></head>"
                '<body><p class="notice" style="font-weight: bold" onclick="bad()">'
                "Formatted message</p><script>unsafe()</script></body></html>"
            ),
        )

        html = build_email_html(email)

        self.assertIn("<style>.notice { color: #123456; }</style>", html)
        self.assertIn('class="notice"', html)
        self.assertIn('style="font-weight: bold;"', html)
        self.assertIn("Formatted message", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("onclick", html)
