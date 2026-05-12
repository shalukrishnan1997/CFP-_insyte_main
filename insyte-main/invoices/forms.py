"""Forms for the invoices admin views."""

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

_INPUT_CLASSES = (
    "w-full p-2 border border-gray-300 rounded-md "
    "focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
)


def _split_emails(raw: str) -> list[str]:
    """Split a comma- or newline-separated email string into trimmed addresses.

    Args:
        raw: User-supplied recipient text.

    Returns:
        List of non-empty trimmed email addresses, in original order.
    """
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        trimmed = chunk.strip()
        if trimmed:
            parts.append(trimmed)
    return parts


def _validate_email_list(addresses: list[str]) -> None:
    """Validate every address in ``addresses`` using Django's email validator.

    Args:
        addresses: Email strings to validate.

    Raises:
        ValidationError: If any address is malformed.
    """
    for address in addresses:
        try:
            validate_email(address)
        except ValidationError as exc:
            msg = f"Invalid email address: {address}"
            raise ValidationError(msg) from exc


class InvoiceSendEmailForm(forms.Form):
    """Form for sending an invoice PDF to one or more recipients."""

    to = forms.CharField(
        label="To",
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": _INPUT_CLASSES,
                "placeholder": "client@example.com, accounts@example.com",
            }
        ),
        help_text="Comma-separated email addresses (at least one required).",
    )
    cc = forms.CharField(
        label="CC",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": _INPUT_CLASSES,
                "placeholder": "Optional comma-separated CC addresses",
            }
        ),
        help_text="Optional comma-separated CC addresses.",
    )
    message = forms.CharField(
        label="Message",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": _INPUT_CLASSES,
                "rows": 5,
                "placeholder": "Optional note included in the email body.",
            }
        ),
        help_text="Optional note included in the email body.",
    )

    def clean_to(self) -> list[str]:
        """Parse and validate the comma-separated ``to`` field.

        Returns:
            List of validated email addresses (always non-empty on success).

        Raises:
            ValidationError: If the field is empty or any address is invalid.
        """
        addresses = _split_emails(self.cleaned_data.get("to", ""))
        if not addresses:
            msg = "At least one recipient is required."
            raise ValidationError(msg)
        _validate_email_list(addresses)
        return addresses

    def clean_cc(self) -> list[str]:
        """Parse and validate the comma-separated ``cc`` field.

        Returns:
            List of validated CC email addresses (may be empty).

        Raises:
            ValidationError: If any address is invalid.
        """
        addresses = _split_emails(self.cleaned_data.get("cc", ""))
        _validate_email_list(addresses)
        return addresses
