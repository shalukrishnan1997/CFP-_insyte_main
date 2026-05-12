"""Authentication and profile forms.

Forms:
    UserProfileForm: Update user profile details.
    ChangePasswordForm: Change password with current password validation.
"""

from django import forms
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError


class UserProfileForm(forms.Form):
    """Form for updating user profile."""

    first_name = forms.CharField(
        max_length=150,
        required=False,
        strip=True,
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        strip=True,
    )
    email = forms.EmailField(
        help_text="Required.",
    )


class ChangePasswordForm(forms.Form):
    """Form for changing user password.

    Validates:
        - Current password is correct
        - New password meets strength requirements
        - Password confirmation matches
    """

    current_password = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Enter your current password.",
    )
    new_password = forms.CharField(
        widget=forms.PasswordInput,
        min_length=8,
        help_text="Minimum 8 characters.",
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Re-enter new password.",
    )

    def clean_new_password(self) -> str:
        """Validate password strength via Django validators."""
        password = self.cleaned_data["new_password"]
        password_validation.validate_password(password)
        return password

    def clean(self) -> dict[str, object] | None:
        """Validate password confirmation matches."""
        cleaned = super().clean()
        if cleaned is None:
            return None
        new_pw = cleaned.get("new_password", "")
        confirm = cleaned.get("confirm_password", "")
        if new_pw and confirm and new_pw != confirm:
            raise ValidationError({"confirm_password": "Passwords do not match."})
        return cleaned
