"""User management forms for staff admin panel.

Forms:
    UserCreateForm: Create new staff/admin users with password validation.
    UserEditForm: Edit existing user details with optional password change.
    GroupForm: Create/edit permission groups.
"""

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError

User = get_user_model()


class UserCreateForm(forms.Form):
    """Form for creating new staff users.

    Validates:
        - Username uniqueness
        - Email uniqueness
        - Password strength via Django validators
        - Password confirmation match
    """

    username = forms.CharField(
        max_length=150,
        strip=True,
        help_text="Required. 150 characters or fewer.",
    )
    email = forms.EmailField(
        help_text="Required. Must be a valid email address.",
    )
    first_name = forms.CharField(
        max_length=150,
        strip=True,
        help_text="Required.",
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        strip=True,
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Required. Must meet password policy.",
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput,
        help_text="Enter the same password for confirmation.",
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(),
        required=False,
    )

    def clean_username(self) -> str:
        """Validate username is unique."""
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self) -> str:
        """Validate email is unique."""
        email = self.cleaned_data["email"]
        if User.objects.filter(email=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_password2(self) -> str:
        """Validate passwords match and meet strength requirements."""
        password1 = self.cleaned_data.get("password1", "")
        password2 = self.cleaned_data["password2"]
        if password1 != password2:
            raise ValidationError("Passwords do not match.")
        password_validation.validate_password(password2)
        return password2


class UserEditForm(forms.Form):
    """Form for editing existing user details.

    Password fields are optional — only validated when provided.
    """

    email = forms.EmailField(
        help_text="Required. Must be a valid email address.",
    )
    first_name = forms.CharField(
        max_length=150,
        strip=True,
        help_text="Required.",
    )
    is_active = forms.BooleanField(required=False)
    is_staff = forms.BooleanField(required=False)
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(),
        required=False,
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave blank to keep current password.",
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
    )

    def clean(self) -> dict[str, object] | None:
        """Validate password match if either field is provided."""
        cleaned = super().clean()
        if cleaned is None:
            return None
        password = cleaned.get("password", "")
        password_confirm = cleaned.get("password_confirm", "")
        if password:
            if password != password_confirm:
                raise ValidationError({"password_confirm": "Passwords do not match."})
            password_validation.validate_password(password)
        return cleaned


class GroupForm(forms.Form):
    """Form for creating/editing permission groups."""

    name = forms.CharField(
        max_length=150,
        strip=True,
        help_text="Required. Group name.",
    )
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.all(),
        required=False,
    )
