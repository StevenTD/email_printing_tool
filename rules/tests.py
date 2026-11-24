from datetime import datetime, time, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from core.models import AppConfiguration, WorkerLog
from core.access import OPERATOR_GROUP_NAME
from mails.models import EmailAttachment, ReceivedEmail
from printing.models import PrintJob, PrinterProfile

from .forms import AcknowledgementTemplateForm, FilterRuleForm
from .matching import find_matching_rule, rule_matches
from .models import AcknowledgementTemplate, FilterRule
from .processing import is_within_auto_print_schedule, process_incoming_email


class RuleMatchingTests(TestCase):
    def setUp(self):
        self.email = ReceivedEmail.objects.create(
            message_id="<rules@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=789,
            imap_uid=1200,
            sender="Operations <ops@example.test>",
            subject="Quarterly report v2",
        )

    def test_sender_domain_and_subject_regex_match_case_insensitively(self):
        rule = FilterRule(
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="EXAMPLE.test",
            subject_match=FilterRule.SubjectMatch.REGEX,
            subject_value=r"^quarterly\s+report\s+v\d+$",
        )

        self.assertTrue(rule_matches(rule, self.email))

    def test_attachment_rule_matches_mime_type_and_extension(self):
        EmailAttachment.objects.create(
            email=self.email,
            filename="Quarterly.PDF",
            content_type="application/pdf",
        )
        mime_rule = FilterRule(attachment_type="application/pdf")
        extension_rule = FilterRule(attachment_type=".pdf")

        self.assertTrue(rule_matches(mime_rule, self.email))
        self.assertTrue(rule_matches(extension_rule, self.email))

    def test_first_matching_enabled_rule_wins_by_priority(self):
        later = FilterRule.objects.create(
            name="Later priority",
            priority=20,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
            action=FilterRule.Action.PRINT,
        )
        first = FilterRule.objects.create(
            name="First priority",
            priority=10,
            subject_match=FilterRule.SubjectMatch.CONTAINS,
            subject_value="quarterly",
            action=FilterRule.Action.HOLD,
        )

        self.assertEqual(find_matching_rule(self.email), first)
        self.assertNotEqual(find_matching_rule(self.email), later)

    def test_rule_form_rejects_invalid_regular_expression(self):
        form = FilterRuleForm(
            data={
                "name": "Bad regex",
                "enabled": "on",
                "priority": "1",
                "sender_match": "any",
                "sender_value": "",
                "subject_match": "regex",
                "subject_value": "[",
                "attachment_type": "",
                "action": "hold",
                "auto_print_override": "",
                "target_printer": "",
                "copies": "1",
                "print_email_body": "on",
                "print_attachments": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Invalid regular expression", str(form.errors))

    def test_rule_form_uses_configured_printer_profiles(self):
        PrinterProfile.objects.create(
            printer_name="Desk Printer",
            alias="Front Desk",
            dedicated_purpose="Orders",
        )
        form = FilterRuleForm(
            data={
                "name": "Print orders",
                "enabled": "on",
                "priority": "1",
                "sender_match": "any",
                "sender_value": "",
                "subject_match": "any",
                "subject_value": "",
                "attachment_type": "",
                "action": "print",
                "auto_print_override": "",
                "target_printer": "Desk Printer",
                "copies": "1",
                "print_email_body": "on",
                "print_attachments": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        rule = form.save()
        self.assertEqual(rule.target_printer, "Desk Printer")


class AcknowledgementTemplateTests(TestCase):
    def test_template_form_rejects_unsupported_variables(self):
        form = AcknowledgementTemplateForm(
            data={
                "name": "Bad template",
                "body": "Hello {unknown_value}",
                "enabled": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Unsupported acknowledgement template variable", str(form.errors))

    def test_tracking_template_accepts_inline_css_and_edats_variables(self):
        form = AcknowledgementTemplateForm(
            data={
                "name": "EDATS HTML",
                "body": "Tracking {tracking_number}",
                "body_html": (
                    "<style>.banner { color: #123456; }</style>"
                    "<p class=\"banner\">{tracking_number}</p>"
                ),
                "enabled": "on",
                "is_tracking_template": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_saving_new_default_clears_previous_default(self):
        previous = AcknowledgementTemplate.objects.create(
            name="Old default",
            body="Old",
            is_default=True,
        )
        form = AcknowledgementTemplateForm(
            data={
                "name": "New default",
                "body": "Received {subject}",
                "enabled": "on",
                "is_default": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        current = form.save()

        previous.refresh_from_db()
        self.assertFalse(previous.is_default)
        self.assertTrue(current.is_default)

    def test_default_template_must_be_enabled(self):
        form = AcknowledgementTemplateForm(
            data={
                "name": "Disabled default",
                "body": "Received {subject}",
                "is_default": "on",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("must be enabled", str(form.errors))

    def test_auto_acknowledgement_cannot_be_enabled_without_default(self):
        user = get_user_model().objects.create_user(
            username="admin",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(user)
        AppConfiguration.objects.create(auto_acknowledge_after_print=False)

        response = self.client.post(
            reverse("rules:toggle_auto_acknowledge"),
            {"enabled": "true"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            AppConfiguration.objects.get(key=1).auto_acknowledge_after_print
        )

    def test_staff_can_manage_templates_and_enable_auto_acknowledgement(self):
        user = get_user_model().objects.create_user(
            username="admin",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(user)
        template = AcknowledgementTemplate.objects.create(
            name="Default",
            body="Received {subject}",
            is_default=True,
        )

        page = self.client.get(reverse("rules:templates"))
        enabled = self.client.post(
            reverse("rules:toggle_auto_acknowledge"),
            {"enabled": "true"},
        )

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Automatic acknowledgement after printing")
        self.assertEqual(enabled.status_code, 302)
        self.assertTrue(
            AppConfiguration.objects.get(key=1).auto_acknowledge_after_print
        )

        blocked_delete = self.client.post(
            reverse("rules:template_delete", args=[template.pk])
        )
        self.assertEqual(blocked_delete.status_code, 302)
        self.assertTrue(AcknowledgementTemplate.objects.filter(pk=template.pk).exists())

    def test_operator_cannot_manage_templates(self):
        user = get_user_model().objects.create_user(
            username="operator",
            password="test-pass",
        )
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        self.client.force_login(user)

        response = self.client.get(reverse("rules:templates"))

        self.assertEqual(response.status_code, 403)


class AutoPrintPolicyTests(TestCase):
    def make_email(self, uid):
        return ReceivedEmail.objects.create(
            message_id=f"<auto-{uid}@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=790,
            imap_uid=uid,
            sender="orders@example.test",
            subject="New order",
        )

    def test_global_auto_print_off_holds_print_rule_even_with_override(self):
        AppConfiguration.objects.create(auto_print_enabled=False)
        email = self.make_email(1201)
        FilterRule.objects.create(
            name="Orders",
            action=FilterRule.Action.PRINT,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
            auto_print_override=True,
        )

        result = process_incoming_email(email)

        email.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(email.status, ReceivedEmail.Status.HELD)
        self.assertFalse(PrintJob.objects.exists())

    def test_global_off_allows_print_rule_with_printable_attachment(self):
        AppConfiguration.objects.create(auto_print_enabled=False)
        email = self.make_email(1206)
        EmailAttachment.objects.create(
            email=email,
            filename="invoice.pdf",
            content_type="application/pdf",
            printable=True,
        )
        FilterRule.objects.create(
            name="Print attachment orders",
            action=FilterRule.Action.PRINT,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
            print_attachments=True,
        )

        job = process_incoming_email(email)

        email.refresh_from_db()
        self.assertEqual(email.status, ReceivedEmail.Status.QUEUED)
        self.assertIsNotNone(job)
        self.assertTrue(job.print_attachments)
        self.assertTrue(
            WorkerLog.objects.filter(
                email=email,
                message__contains="while global auto-print is off",
            ).exists()
        )

    def test_global_off_does_not_bypass_rule_that_disables_attachments(self):
        AppConfiguration.objects.create(auto_print_enabled=False)
        email = self.make_email(1207)
        EmailAttachment.objects.create(
            email=email,
            filename="invoice.pdf",
            content_type="application/pdf",
            printable=True,
        )
        FilterRule.objects.create(
            name="Do not print attachments",
            action=FilterRule.Action.PRINT,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
            print_attachments=False,
        )

        job = process_incoming_email(email)

        email.refresh_from_db()
        self.assertIsNone(job)
        self.assertEqual(email.status, ReceivedEmail.Status.HELD)

    def test_global_on_queues_matching_rule_with_its_print_options(self):
        AppConfiguration.objects.create(auto_print_enabled=True)
        email = self.make_email(1202)
        FilterRule.objects.create(
            name="Orders",
            action=FilterRule.Action.PRINT,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
            target_printer="Front Desk",
            copies=2,
            print_email_body=False,
            print_attachments=True,
        )

        job = process_incoming_email(email)

        email.refresh_from_db()
        self.assertEqual(email.status, ReceivedEmail.Status.QUEUED)
        self.assertTrue(job.automatic)
        self.assertEqual(job.printer_name, "Front Desk")
        self.assertEqual(job.copies, 2)
        self.assertFalse(job.print_email_body)
        self.assertTrue(job.print_attachments)

    def test_schedule_holds_out_of_window_unless_rule_bypasses_schedule(self):
        configuration = AppConfiguration.objects.create(
            auto_print_enabled=True,
            schedule_enabled=True,
            schedule_weekdays=[1],
            schedule_start=time(8, 0),
            schedule_end=time(17, 0),
        )
        tuesday_evening = datetime(2026, 1, 6, 20, 0, tzinfo=datetime_timezone.utc)
        self.assertFalse(
            is_within_auto_print_schedule(configuration, now=tuesday_evening)
        )

        email = self.make_email(1203)
        rule = FilterRule.objects.create(
            name="Scheduled",
            action=FilterRule.Action.PRINT,
            sender_match=FilterRule.SenderMatch.DOMAIN,
            sender_value="example.test",
        )
        process_incoming_email(email, now=tuesday_evening)
        email.refresh_from_db()
        self.assertEqual(email.status, ReceivedEmail.Status.HELD)

        rule.auto_print_override = True
        rule.save(update_fields=["auto_print_override"])
        second_email = self.make_email(1204)
        job = process_incoming_email(second_email, now=tuesday_evening)
        self.assertTrue(job.automatic)

    def test_default_ignore_action_skips_unmatched_email(self):
        AppConfiguration.objects.create(default_action=AppConfiguration.DefaultAction.IGNORE)
        email = self.make_email(1205)

        process_incoming_email(email)

        email.refresh_from_db()
        self.assertEqual(email.status, ReceivedEmail.Status.SKIPPED)


class AutoPrintToggleViewTests(TestCase):
    def test_turning_auto_print_off_cancels_pending_automatic_jobs_only(self):
        user = get_user_model().objects.create_user(
            username="admin",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(user)
        AppConfiguration.objects.create(auto_print_enabled=True)
        automatic_email = ReceivedEmail.objects.create(
            message_id="<pending-auto@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=791,
            imap_uid=1301,
            status=ReceivedEmail.Status.QUEUED,
        )
        manual_email = ReceivedEmail.objects.create(
            message_id="<pending-manual@example.test>",
            imap_folder="INBOX",
            imap_uidvalidity=791,
            imap_uid=1302,
            status=ReceivedEmail.Status.QUEUED,
        )
        automatic_job = PrintJob.objects.create(email=automatic_email, automatic=True)
        manual_job = PrintJob.objects.create(email=manual_email, automatic=False)

        response = self.client.post(
            reverse("core:toggle_auto_print"),
            {"enabled": "false"},
        )

        automatic_job.refresh_from_db()
        manual_job.refresh_from_db()
        automatic_email.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(automatic_job.status, PrintJob.Status.CANCELLED)
        self.assertEqual(automatic_email.status, ReceivedEmail.Status.HELD)
        self.assertEqual(manual_job.status, PrintJob.Status.QUEUED)
        self.assertFalse(AppConfiguration.objects.get(key=1).auto_print_enabled)

    def test_operator_cannot_change_auto_print(self):
        user = get_user_model().objects.create_user(username="operator", password="test-pass")
        operator_group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
        user.groups.add(operator_group)
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:toggle_auto_print"),
            {"enabled": "true"},
        )

        self.assertEqual(response.status_code, 403)
