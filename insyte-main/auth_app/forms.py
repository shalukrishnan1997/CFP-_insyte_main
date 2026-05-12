from django import forms

from core.models import User


class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full p-2 border rounded focus:ring-2 focus:ring-(--primary-color)"
            }
        )
    )

    class Meta:
        model = User
        fields = ["username", "email", "password", "first_name", "last_name", "groups"]
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
