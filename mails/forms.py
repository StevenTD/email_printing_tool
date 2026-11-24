from django import forms

from rules.models import AcknowledgementTemplate


class AcknowledgementSendForm(forms.Form):
    template = forms.ModelChoiceField(
        queryset=AcknowledgementTemplate.objects.none(),
        label="Acknowledgement template",
    )
    confirm_duplicate = forms.BooleanField(
        required=False,
        label="I confirm sending another acknowledgement",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        templates = AcknowledgementTemplate.objects.filter(
            enabled=True,
            is_tracking_template=False,
        ).order_by("name")
        self.fields["template"].queryset = templates
        if not self.is_bound:
            default_template = templates.filter(is_default=True).first() or templates.first()
            if default_template:
                self.initial["template"] = default_template
        self.fields["template"].widget.attrs["class"] = "form-select"
        self.fields["confirm_duplicate"].widget.attrs["class"] = "form-check-input"


class EdatsAcknowledgementSendForm(AcknowledgementSendForm):
    tracking_number = forms.CharField(
        max_length=100,
        label="EDATS tracking number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "P.AKLAN2026-10184",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        templates = AcknowledgementTemplate.objects.filter(
            enabled=True,
            is_tracking_template=True,
        ).order_by("name")
        self.fields["template"].queryset = templates
        self.fields["template"].label = "EDATS acknowledgement template"
        if not self.is_bound:
            default_template = templates.first()
            if default_template:
                self.initial["template"] = default_template
