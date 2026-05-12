"""Letter setup forms for campaign-specific DOCX uploads."""

from django import forms
from django.core.exceptions import ValidationError

TEMPLATE_TYPE_CHOICES = [
    ("thank_you", "Thank You"),
    ("issue", "Issue"),
]


class LetterTemplateUploadForm(forms.Form):
    """Form for uploading DOCX templates to campaigns.

    Validates:
        - File is a DOCX
        - File size within limits
    """

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    file = forms.FileField(
        help_text="Upload a .docx template file (max 10 MB).",
    )
    template_name = forms.CharField(  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        max_length=255,
        required=False,
        strip=True,
        initial="Template",
    )
    template_type = forms.ChoiceField(
        choices=TEMPLATE_TYPE_CHOICES,
        initial="thank_you",
        required=False,
    )

    def clean_file(self) -> object:
        """Validate file type and size."""
        uploaded = self.cleaned_data["file"]
        if not uploaded.name.lower().endswith(".docx"):
            raise ValidationError("Only .docx files are supported.")
        if uploaded.size > self.MAX_FILE_SIZE:
            raise ValidationError(
                f"File too large. Maximum size is {self.MAX_FILE_SIZE // (1024 * 1024)} MB."
            )
        return uploaded
