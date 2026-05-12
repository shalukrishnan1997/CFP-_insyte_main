"""Client and portal user forms.

Forms:
    ClientForm: Create/edit charity client details.
    PortalUserCreateForm: Create client portal users with validation.
    PortalUserResetPasswordForm: Reset portal user passwords.
    PaymentGatewayConfigForm: Configure payment gateway for a client.
"""

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError

User = get_user_model()

PORTAL_ROLE_CHOICES = [
    ("viewer", "Viewer"),
    ("manager", "Manager"),
    ("admin", "Admin"),
]


class ClientForm(forms.Form):
    """Form for creating/editing charity clients.

    Handles both client details and logo upload.
    """

    name = forms.CharField(
        max_length=255,
        strip=True,
        help_text="Charity/organisation name.",
    )
    client_code = forms.CharField(
        max_length=20,
        required=False,
        strip=True,
        help_text="Short uppercase code (e.g. 'BRC'). Used in R2 scan folder paths.",
    )
    email = forms.EmailField(
        required=False,
        help_text="Primary contact email.",
    )
    phone = forms.CharField(max_length=30, required=False, strip=True)
    description = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    website = forms.URLField(required=False, assume_scheme="https")
    address_line1 = forms.CharField(max_length=255, required=False, strip=True)
    address_line2 = forms.CharField(max_length=255, required=False, strip=True)
    city = forms.CharField(max_length=100, required=False, strip=True)
    postal_code = forms.CharField(max_length=20, required=False, strip=True)
    country = forms.CharField(max_length=100, required=False, strip=True)
    is_active = forms.BooleanField(required=False, initial=True)
    logo = forms.ImageField(required=False)


class PortalUserCreateForm(forms.Form):
    """Form for creating client portal users.

    Validates:
        - Username uniqueness
        - Email uniqueness
        - Password strength
    """

    username = forms.CharField(
        max_length=150,
        strip=True,
        help_text="Required. Must be unique.",
    )
    email = forms.EmailField(
        help_text="Required. Must be unique.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Required. Must meet password policy.",
    )
    first_name = forms.CharField(max_length=150, required=False, strip=True)
    last_name = forms.CharField(max_length=150, required=False, strip=True)
    role = forms.ChoiceField(
        choices=PORTAL_ROLE_CHOICES,
        initial="viewer",
    )
    is_primary = forms.BooleanField(required=False)

    def clean_username(self) -> str:
        """Validate username is unique."""
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise ValidationError("Username already exists.")
        return username

    def clean_email(self) -> str:
        """Validate email is unique."""
        email = self.cleaned_data["email"]
        if User.objects.filter(email=email).exists():
            raise ValidationError("Email already exists.")
        return email

    def clean_password(self) -> str:
        """Validate password strength."""
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password


class PortalUserResetPasswordForm(forms.Form):
    """Form for resetting a portal user's password."""

    new_password = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Required. Must meet password policy.",
    )

    def clean_new_password(self) -> str:
        """Validate password strength."""
        password = self.cleaned_data["new_password"]
        password_validation.validate_password(password)
        return password


class PaymentGatewayConfigForm(forms.Form):
    """Form for configuring payment gateway per client.

    Dynamic fields based on selected provider.
    """

    PROVIDER_CHOICES = [
        ("stripe", "Stripe"),
        ("sagepay", "SagePay"),
        ("worldpay", "WorldPay"),
    ]

    provider = forms.ChoiceField(choices=PROVIDER_CHOICES)
    is_active = forms.BooleanField(required=False, initial=True)

    # Stripe fields
    stripe_secret_key = forms.CharField(max_length=255, required=False, strip=True)
    stripe_publishable_key = forms.CharField(max_length=255, required=False, strip=True)
    stripe_webhook_secret = forms.CharField(max_length=255, required=False, strip=True)

    # SagePay fields
    sagepay_vendor = forms.CharField(max_length=255, required=False, strip=True)
    sagepay_key = forms.CharField(max_length=255, required=False, strip=True)
    sagepay_password = forms.CharField(
        max_length=255, required=False, widget=forms.PasswordInput
    )

    # WorldPay fields
    worldpay_inst_id = forms.CharField(max_length=255, required=False, strip=True)
    worldpay_merchant = forms.CharField(max_length=255, required=False, strip=True)
    worldpay_api_key = forms.CharField(max_length=255, required=False, strip=True)
