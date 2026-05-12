"""Invoice management forms.

Forms:
    InvoiceCreateForm: Create invoices with service line items.
    InvoiceMarkPaidForm: Record invoice payment.
    InvoiceChangeStatusForm: Change invoice status.
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


class InvoiceCreateForm(forms.Form):
    """Form for creating invoices with service line items.

    Validates:
        - Client and campaign selection
        - Billing period dates
        - Service line items JSON structure
    """

    client_id = forms.UUIDField(help_text="Client to invoice.")
    campaign_id = forms.UUIDField(help_text="Campaign for this invoice.")
    billing_period_start = forms.CharField(
        help_text="Billing period start (DD/MM/YYYY)."
    )
    billing_period_end = forms.CharField(help_text="Billing period end (DD/MM/YYYY).")
    due_days = forms.IntegerField(
        required=False,
        initial=30,
        min_value=1,
        max_value=365,
        help_text="Payment due in N days from issue date.",
    )
    tax_rate = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        initial=0,
        help_text="VAT rate percentage (e.g., 20 for 20%).",
    )
    notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    terms_and_conditions = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    selected_services = forms.CharField(
        required=False,
        help_text="JSON array of service line items.",
    )

    def clean_billing_period_start(self) -> date:
        """Parse billing start date."""
        parsed = _parse_date(self.cleaned_data["billing_period_start"])
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed

    def clean_billing_period_end(self) -> date:
        """Parse billing end date."""
        parsed = _parse_date(self.cleaned_data["billing_period_end"])
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed

    def clean_selected_services(self) -> list[dict[str, object]]:
        """Parse and validate service line items JSON."""
        raw = self.cleaned_data.get("selected_services", "")
        if not raw or not raw.strip():
            return []
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValidationError("Must be a JSON array.")
            return items  # type: ignore[return-value]
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON: {exc}") from exc

    def clean(self) -> dict[str, object] | None:
        """Validate billing period range."""
        cleaned = super().clean()
        if cleaned is None:
            return None
        start = cleaned.get("billing_period_start")
        end = cleaned.get("billing_period_end")
        if start and end and start > end:
            raise ValidationError(
                {"billing_period_end": "End date must be after start date."}
            )
        return cleaned


class InvoiceMarkPaidForm(forms.Form):
    """Form for recording invoice payment."""

    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        help_text="Amount paid (defaults to invoice total).",
    )
    payment_method = forms.CharField(
        max_length=50,
        required=False,
        strip=True,
    )
    payment_reference = forms.CharField(
        max_length=255,
        required=False,
        strip=True,
        help_text="Bank reference or transaction ID.",
    )
    payment_date = forms.CharField(
        required=False,
        help_text="Payment date (DD/MM/YYYY). Defaults to today.",
    )

    def clean_payment_date(self) -> date | None:
        """Parse payment date if provided."""
        raw = self.cleaned_data.get("payment_date", "")
        if not raw or not raw.strip():
            return None
        parsed = _parse_date(raw)
        if not parsed:
            raise ValidationError("Invalid date format. Use DD/MM/YYYY.")
        return parsed


class InvoiceChangeStatusForm(forms.Form):
    """Form for changing invoice status."""

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("issued", "Issued"),
        ("overdue", "Overdue"),
        ("cancelled", "Cancelled"),
    ]

    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        help_text="New invoice status.",
    )
