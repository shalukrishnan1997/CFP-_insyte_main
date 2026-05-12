"""Daily banking / paying-in slip forms.

Forms:
    PayingInSlipCreateForm: Create paying-in slips.
    PayingInSlipEditForm: Edit slip details.
    SlipStatusForm: Change slip status.
    SlipProcessingForm: Record bank processing details.
"""

import json
from datetime import date, datetime

from django import forms
from django.core.exceptions import ValidationError


def _parse_date(value: str) -> date | None:
    """Parse UK or ISO date formats."""
    if not value or not value.strip():
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


class PayingInSlipCreateForm(forms.Form):
    """Form for creating paying-in slips.

    Validates:
        - Unique slip number
        - At least one batch or donation selected
        - JSON format for IDs
    """

    slip_number = forms.CharField(
        max_length=50,
        strip=True,
        help_text="Unique paying-in slip reference number.",
    )
    banking_date = forms.CharField(
        required=False,
        help_text="Banking date (DD/MM/YYYY).",
    )
    notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    batch_ids = forms.CharField(
        required=False,
        help_text="JSON array of batch IDs.",
    )
    donation_ids = forms.CharField(
        required=False,
        help_text="JSON array of donation UUIDs.",
    )

    def clean_banking_date(self) -> date | None:
        """Parse banking date."""
        raw = self.cleaned_data.get("banking_date", "")
        if not raw or not raw.strip():
            return None
        parsed = _parse_date(raw)
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed

    def clean_batch_ids(self) -> list[int]:
        """Parse batch IDs JSON."""
        raw = self.cleaned_data.get("batch_ids", "")
        if not raw or not raw.strip():
            return []
        try:
            ids = json.loads(raw)
            if not isinstance(ids, list):
                raise ValidationError("Must be a JSON array.")
            return [int(i) for i in ids]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValidationError(f"Invalid batch IDs: {exc}") from exc

    def clean_donation_ids(self) -> list[str]:
        """Parse donation UUIDs JSON."""
        raw = self.cleaned_data.get("donation_ids", "")
        if not raw or not raw.strip():
            return []
        try:
            ids = json.loads(raw)
            if not isinstance(ids, list):
                raise ValidationError("Must be a JSON array.")
            return [str(i) for i in ids]
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(f"Invalid donation IDs: {exc}") from exc

    def clean(self) -> dict[str, object] | None:
        """Ensure at least one batch or donation is selected."""
        cleaned = super().clean()
        if cleaned is None:
            return None
        batch_ids = cleaned.get("batch_ids", [])
        donation_ids = cleaned.get("donation_ids", [])
        if not batch_ids and not donation_ids:
            raise ValidationError("At least one batch or donation must be selected.")
        return cleaned


class PayingInSlipEditForm(forms.Form):
    """Form for editing paying-in slip details."""

    slip_number = forms.CharField(
        max_length=50,
        required=False,
        strip=True,
    )
    banking_date = forms.CharField(
        required=False,
        help_text="Banking date (YYYY-MM-DD).",
    )
    notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )

    def clean_banking_date(self) -> date | None:
        """Parse banking date."""
        raw = self.cleaned_data.get("banking_date", "")
        if not raw or not raw.strip():
            return None
        parsed = _parse_date(raw)
        if not parsed:
            raise ValidationError("Invalid date format.")
        return parsed


class SlipStatusForm(forms.Form):
    """Form for changing slip status."""

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("ready", "Ready for Banking"),
        ("banked", "Banked"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        help_text="New slip status.",
    )


class SlipProcessingForm(forms.Form):
    """Form for recording bank processing results."""

    processed_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        initial=0,
        help_text="Amount processed by the bank (£).",
    )
    completion_status = forms.ChoiceField(
        choices=[
            ("full", "Fully Processed"),
            ("partial", "Partially Processed"),
            ("rejected", "Rejected by Bank"),
        ],
        help_text="Processing outcome.",
    )
    bank_processed_date = forms.CharField(
        required=False,
        help_text="Date processed by bank (DD/MM/YYYY).",
    )
    processing_issues = forms.CharField(
        required=False,
        help_text="JSON array of processing issues.",
    )
    custom_issue = forms.CharField(
        max_length=500,
        required=False,
        strip=True,
    )

    def clean_bank_processed_date(self) -> date | None:
        """Parse bank processing date."""
        raw = self.cleaned_data.get("bank_processed_date", "")
        if not raw or not raw.strip():
            return None
        parsed = _parse_date(raw)
        if not parsed:
            raise ValidationError("Invalid date format.")
        return parsed

    def clean_processing_issues(self) -> list[str]:
        """Parse processing issues JSON."""
        raw = self.cleaned_data.get("processing_issues", "")
        if not raw or not raw.strip():
            return []
        try:
            issues = json.loads(raw)
            if not isinstance(issues, list):
                raise ValidationError("Must be a JSON array.")
            return [str(i) for i in issues]
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON: {exc}") from exc
