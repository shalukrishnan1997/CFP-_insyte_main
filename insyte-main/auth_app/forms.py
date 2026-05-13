from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from core.models import User


class UserRegistrationForm(forms.ModelForm):
    """Create a user with a hashed password (never stores plaintext)."""

    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
            }
        ),
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
            }
        ),
    )

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "groups"]
        widgets = {
            "username": forms.TextInput(
                attrs={
                    "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
                }
            ),
            "first_name": forms.TextInput(
                attrs={
                    "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
                }
            ),
            "groups": forms.Select(
                attrs={
                    "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
                }
            ),
        }

    def clean(self):
        """Ensure matching passwords and Django password policy."""
        cleaned_data = super().clean()
        pw = cleaned_data.get("password")
        pw2 = cleaned_data.get("password_confirm")
        if pw is None or pw2 is None:
            return cleaned_data
        if pw != pw2:
            raise ValidationError(
                {"password_confirm": "The two password fields do not match."}
            )
        validate_password(pw, user=self.instance)
        return cleaned_data

    def save(self, commit=True):
        """Persist the user with a hashed ``password`` field."""
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
            self.save_m2m()
        return user
