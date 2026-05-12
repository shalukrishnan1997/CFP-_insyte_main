"""Campaign management forms.

Forms:
    CampaignCreateForm: Create new campaigns with date/package validation.
    CampaignEditForm: Edit existing campaigns (adds status field).
"""

import json
from datetime import date, datetime

from django import forms
from django.core.exceptions import ValidationError


def _parse_uk_date(value: str) -> date | None:
    """Parse UK date formats (DD/MM/YYYY) or ISO (YYYY-MM-DD).

    Args:
        value: Date string in DD/MM/YYYY or YYYY-MM-DD format.

    Returns:
        Parsed date or None if empty/invalid.
    """
    if not value or not value.strip():
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


APPEAL_TYPE_CHOICES = [
    ("Donation", "Donation"),
    ("Raffle", "Raffle"),
]


class CampaignCreateForm(forms.Form):
    """Form for creating new campaigns.

    Validates:
        - Required fields (title, client, appeal code, dates)
        - UK date parsing
        - Package code format
        - Extra fields JSON structure
    """

    title = forms.CharField(
        max_length=255,
        strip=True,
        help_text="Campaign title. Must be unique per client.",
    )
    description = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    client_id = forms.UUIDField(
        help_text="Client/charity this campaign belongs to.",
    )
    appeal_code = forms.CharField(
        max_length=50,
        strip=True,
        help_text="Unique appeal reference code.",
    )
    package_codes = forms.CharField(
        strip=True,
        help_text="Comma-separated package codes (e.g., 'PKG1, PKG2').",
    )
    appeal_type = forms.ChoiceField(
        choices=APPEAL_TYPE_CHOICES,
        initial="Donation",
    )
    appeal_start = forms.CharField(
        help_text="Start date (DD/MM/YYYY).",
    )
    appeal_end = forms.CharField(
        help_text="End date (DD/MM/YYYY).",
    )
    hgv_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        initial=0,
        help_text="High Gift Value threshold amount (£).",
    )
    lgv_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        initial=0,
        help_text="Low Gift Value threshold amount (£).",
    )
    campaign_manager_emails = forms.CharField(
        required=False,
        strip=True,
        help_text="Comma-separated manager email addresses.",
    )
    extra_fields_json = forms.CharField(
        required=False,
        help_text="JSON array of campaign-specific fields.",
    )
    campaign_temperature = forms.ChoiceField(
        choices=[],  # populated from Campaign.CAMPAIGN_TEMPERATURE_CHOICES
        initial="cold",
        help_text="Whether this campaign is a warm or cold campaign.",
    )
    scan_purpose = forms.ChoiceField(
        choices=[],  # populated from Campaign.SCAN_PURPOSE_CHOICES at runtime
        required=False,
        initial="donation",
        help_text="What the scan data is used for.",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Inject dynamic choice lists from Campaign model constants."""
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        from campaigns.models import Campaign

        self.fields[
            "campaign_temperature"
        ].choices = Campaign.CAMPAIGN_TEMPERATURE_CHOICES  # type: ignore[attr-defined]
        self.fields["scan_purpose"].choices = Campaign.SCAN_PURPOSE_CHOICES  # type: ignore[attr-defined]

    def clean_package_codes(self) -> list[str]:
        """Parse and deduplicate comma-separated package codes."""
        raw = self.cleaned_data["package_codes"]
        codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
        if not codes:
            raise ValidationError("At least one package code is required.")
        return codes

    def clean_appeal_start(self) -> date:
        """Parse UK date format."""
        parsed = _parse_uk_date(self.cleaned_data["appeal_start"])
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed

    def clean_appeal_end(self) -> date:
        """Parse UK date format."""
        parsed = _parse_uk_date(self.cleaned_data["appeal_end"])
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed

    def clean_extra_fields_json(self) -> list[dict[str, object]]:
        """Parse and validate extra fields JSON."""
        raw = self.cleaned_data.get("extra_fields_json", "")
        if not raw or not raw.strip():
            return []
        try:
            fields = json.loads(raw)
            if not isinstance(fields, list):
                raise ValidationError("Must be a JSON array.")
            return fields  # type: ignore[return-value]
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON: {exc}") from exc

    def clean(self) -> dict[str, object] | None:
        """Validate date range."""
        cleaned = super().clean()
        if cleaned is None:
            return None
        start = cleaned.get("appeal_start")
        end = cleaned.get("appeal_end")
        if start and end and start > end:
            raise ValidationError(
                {"appeal_end": "End date must be on or after start date."}
            )
        return cleaned


class CampaignEditForm(CampaignCreateForm):
    """Form for editing existing campaigns.

    Extends CampaignCreateForm with status field.
    """

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("active", "Active"),
        ("paused", "Paused"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        required=False,
        help_text="Campaign status.",
    )
