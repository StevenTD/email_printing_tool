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
        templates = AcknowledgementTemplate.objects.filter(enabled=True).order_by("name")
        self.fields["template"].queryset = templates
        if not self.is_bound:
            default_template = templates.filter(is_default=True).first() or templates.first()
            if default_template:
                self.initial["template"] = default_template
        self.fields["template"].widget.attrs["class"] = "form-select"
        self.fields["confirm_duplicate"].widget.attrs["class"] = "form-check-input"
