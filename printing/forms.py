import re

from django import forms

from core.models import AppConfiguration

from .models import PrinterProfile
from .printers import save_printer_profile


class SumatraPathForm(forms.ModelForm):
    class Meta:
        model = AppConfiguration
        fields = ["sumatra_pdf_path"]
        widgets = {
            "sumatra_pdf_path": forms.TextInput(
                attrs={"placeholder": "SumatraPDF.exe or full path"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sumatra_pdf_path"].label = "SumatraPDF executable"
        self.fields["sumatra_pdf_path"].help_text = (
            "Leave blank to use SUMATRA_PDF_PATH from .env or search PATH."
        )
        self.fields["sumatra_pdf_path"].widget.attrs["class"] = "form-control"


class PrinterProfileForm(forms.ModelForm):
    printer_name = forms.ChoiceField(label="Windows printer")
    paper_size = forms.CharField(
        max_length=30,
        help_text="Paper name understood by the printer driver, for example A4 or Letter.",
    )

    class Meta:
        model = PrinterProfile
        fields = [
            "printer_name",
            "alias",
            "dedicated_purpose",
            "is_default",
            "paper_size",
            "duplex",
            "color_mode",
        ]

    def __init__(self, *args, printer_names=(), **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(name, name) for name in sorted(set(printer_names), key=str.casefold)]
        if self.instance.pk and self.instance.printer_name not in dict(choices):
            choices.append((self.instance.printer_name, f"{self.instance.printer_name} (offline)"))
        self.fields["printer_name"].choices = [("", "Select a printer"), *choices]
        self.fields["printer_name"].widget.attrs["class"] = "form-select"
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        if self.cleaned_data.get("is_default"):
            exclude.add("is_default")
        return exclude

    def clean_paper_size(self):
        paper_size = self.cleaned_data["paper_size"].strip()
        if not re.fullmatch(r"[A-Za-z0-9 _-]{1,30}", paper_size):
            raise forms.ValidationError(
                "Use letters, numbers, spaces, hyphens, or underscores only."
            )
        return paper_size

    def save(self, commit=True):
        profile = super().save(commit=False)
        if commit:
            save_printer_profile(profile)
            self.save_m2m()
        return profile
