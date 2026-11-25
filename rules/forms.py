import re

from django import forms

from core.models import AppConfiguration
from mails.acknowledgements import (
    AcknowledgementError,
    validate_acknowledgement_html_template,
    validate_acknowledgement_template,
)

from .models import AcknowledgementTemplate, FilterRule
from printing.models import PrinterProfile


class FilterRuleForm(forms.ModelForm):
    AUTO_PRINT_OPTIONS = [
        ("", "Follow global switch and schedule"),
        ("true", "Allow automatic print outside the schedule"),
        ("false", "Never automatically print this rule"),
    ]

    auto_print_override = forms.ChoiceField(
        choices=AUTO_PRINT_OPTIONS,
        required=False,
        label="Automatic printing override",
        help_text=(
            "The global OFF switch always stops automatic printing. "
            "Allowing outside the schedule only applies while the global switch is ON."
        ),
    )
    target_printer = forms.ChoiceField(
        choices=[("", "Use application default printer")],
        required=False,
        label="Target printer",
    )

    class Meta:
        model = FilterRule
        fields = [
            "name",
            "enabled",
            "priority",
            "sender_match",
            "sender_value",
            "subject_match",
            "subject_value",
            "attachment_type",
            "action",
            "auto_print_override",
            "target_printer",
            "copies",
            "print_email_body",
            "print_attachments",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        override = self.instance.auto_print_override
        self.initial["auto_print_override"] = (
            "true" if override is True else "false" if override is False else ""
        )
        printer_choices = [
            (
                profile.printer_name,
                " — ".join(
                    label
                    for label in (
                        profile.alias or profile.printer_name,
                        profile.dedicated_purpose,
                    )
                    if label
                ),
            )
            for profile in PrinterProfile.objects.all()
        ]
        if self.instance.target_printer and self.instance.target_printer not in dict(
            printer_choices
        ):
            printer_choices.append(
                (self.instance.target_printer, f"{self.instance.target_printer} (unconfigured)")
            )
        self.fields["target_printer"].choices = [
            ("", "Use application default printer"),
            *printer_choices,
        ]
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        sender_match = cleaned_data.get("sender_match")
        sender_value = cleaned_data.get("sender_value", "").strip()
        if sender_match in (FilterRule.SenderMatch.EXACT, FilterRule.SenderMatch.DOMAIN) and not sender_value:
            self.add_error("sender_value", "Enter a sender address or domain for this match type.")

        subject_match = cleaned_data.get("subject_match")
        subject_value = cleaned_data.get("subject_value", "").strip()
        if subject_match in (FilterRule.SubjectMatch.CONTAINS, FilterRule.SubjectMatch.REGEX) and not subject_value:
            self.add_error("subject_value", "Enter a keyword or regular expression.")
        if subject_match == FilterRule.SubjectMatch.REGEX and subject_value:
            try:
                re.compile(subject_value)
            except re.error as exc:
                self.add_error("subject_value", f"Invalid regular expression: {exc}")

        if (
            cleaned_data.get("action") == FilterRule.Action.PRINT
            and not cleaned_data.get("print_email_body")
            and not cleaned_data.get("print_attachments")
        ):
            raise forms.ValidationError("A print rule must include the email body or attachments.")
        return cleaned_data

    def save(self, commit=True):
        self.instance.auto_print_override = {
            "": None,
            "true": True,
            "false": False,
        }[self.cleaned_data["auto_print_override"]]
        return super().save(commit=commit)


class RuleScheduleForm(forms.Form):
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    enabled = forms.BooleanField(required=False, label="Enable automatic print schedule")
    default_action = forms.ChoiceField(
        choices=AppConfiguration.DefaultAction.choices,
        label="Default action when no rule matches",
    )
    weekdays = forms.TypedMultipleChoiceField(
        choices=WEEKDAYS,
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Allowed weekdays",
    )
    start = forms.TimeField(
        required=False,
        input_formats=["%H:%M"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        label="Start time",
    )
    end = forms.TimeField(
        required=False,
        input_formats=["%H:%M"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        label="End time",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "enabled":
                field.widget.attrs["class"] = "form-check-input"
            elif name == "weekdays":
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("enabled"):
            if not cleaned_data.get("weekdays"):
                self.add_error("weekdays", "Choose at least one weekday.")
            if not cleaned_data.get("start") or not cleaned_data.get("end"):
                raise forms.ValidationError("Choose both a start and end time.")
            if cleaned_data["start"] >= cleaned_data["end"]:
                self.add_error("end", "End time must be later than the start time.")
        return cleaned_data


def schedule_form_for(configuration, data=None):
    return RuleScheduleForm(
        data=data,
        initial={
            "enabled": configuration.schedule_enabled,
            "default_action": configuration.default_action,
            "weekdays": configuration.schedule_weekdays,
            "start": configuration.schedule_start,
            "end": configuration.schedule_end,
        },
    )


class AcknowledgementTemplateForm(forms.ModelForm):
    class Meta:
        model = AcknowledgementTemplate
        fields = [
            "name",
            "body",
            "body_html",
            "enabled",
            "is_default",
            "is_tracking_template",
        ]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 8}),
            "body_html": forms.Textarea(attrs={"rows": 16}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].help_text = (
            "Use {user_department} to include the sending user's department. "
            "It is blank for automatic acknowledgements."
        )
        self.fields["body_html"].help_text = (
            "Optional HTML version; required for EDATS tracking templates. "
            "Inline styles work best in email clients."
        )
        self.fields["is_tracking_template"].help_text = (
            "Use this template for EDATS tracking acknowledgements rather than ordinary replies."
        )
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control font-monospace"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_body(self):
        body = self.cleaned_data["body"]
        try:
            validate_acknowledgement_template(body)
        except AcknowledgementError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return body

    def clean_body_html(self):
        body_html = self.cleaned_data["body_html"]
        if body_html:
            try:
                validate_acknowledgement_html_template(body_html)
            except AcknowledgementError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return body_html

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("is_default") and not cleaned_data.get("enabled"):
            self.add_error("enabled", "The default acknowledgement template must be enabled.")
        if cleaned_data.get("is_default") and cleaned_data.get("is_tracking_template"):
            self.add_error(
                "is_default",
                "An EDATS tracking template cannot be the automatic acknowledgement default.",
            )
        if cleaned_data.get("is_tracking_template") and not cleaned_data.get("body_html"):
            self.add_error(
                "body_html",
                "EDATS tracking templates require an HTML message body.",
            )
        return cleaned_data

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        if self.cleaned_data.get("is_default"):
            exclude.add("is_default")
        return exclude

    def save(self, commit=True):
        template = super().save(commit=False)
        if commit:
            from django.db import transaction

            with transaction.atomic():
                if template.is_default:
                    AcknowledgementTemplate.objects.exclude(pk=template.pk).update(
                        is_default=False
                    )
                template.save()
                self.save_m2m()
        return template
