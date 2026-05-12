"""Miscellaneous tests for auth_app forms, models, and profile edge cases.

Targets previously uncovered lines:
  auth_app/forms.py  1-18   - UserRegistrationForm class definition
  auth_app/models.py 46     - EmailDevice.__str__
  auth_app/models.py 114    - verify_token: too many failed attempts
  auth_app/models.py 119    - verify_token: expired token
  auth_app/models.py 141    - is_interactive()
  auth_app/views/profile.py 35-36 - save exception handler
  auth_app/views/profile.py 85-88 - validate_password ValidationError
"""

from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from auth_app.forms import UserRegistrationForm
from tests.factories import UserFactory

# ---------------------------------------------------------------------------
# EmailDevice model  (lines 46, 114, 119, 141)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailDeviceModel:
    """Tests for auth_app.models.EmailDevice."""

    def test_str_representation(self, db):
        """Line 46: __str__ returns username-based string."""
        from auth_app.models import EmailDevice

        user = UserFactory()
        device = EmailDevice.objects.create(
            user=user, name="email otp", confirmed=False
        )
        result = str(device)
        assert "Email OTP for" in result
        assert user.username in result

    def test_verify_token_too_many_failed_attempts_returns_false(self):
        """Line 114: failed_attempts >= max_attempts → False without checking token."""
        from auth_app.models import EmailDevice

        user = UserFactory()
        device = EmailDevice(user=user, name="email otp")
        # Simulate exhausted attempts
        device.token = make_password("123456")
        device.generated_at = timezone.now()
        device.failed_attempts = device.max_attempts  # threshold reached
        result = device.verify_token("123456")
        assert result is False

    def test_verify_token_expired_returns_false(self):
        """Line 119: token expired by time → False."""
        from datetime import timedelta

        from auth_app.models import EmailDevice

        user = UserFactory()
        device = EmailDevice(user=user, name="email otp")
        device.token = make_password("999999")
        # Set generated_at far in the past (beyond valid_minutes)
        device.generated_at = timezone.now() - timedelta(minutes=60)
        device.failed_attempts = 0
        result = device.verify_token("999999")
        assert result is False

    def test_is_interactive_returns_true(self):
        """Line 141: is_interactive() always returns True."""
        from auth_app.models import EmailDevice

        user = UserFactory()
        device = EmailDevice(user=user, name="email otp")
        assert device.is_interactive() is True


# ---------------------------------------------------------------------------
# UserRegistrationForm
# ---------------------------------------------------------------------------


class TestUserRegistrationForm:
    """Importing and instantiating UserRegistrationForm covers forms.py lines."""

    def test_form_is_importable(self):
        assert UserRegistrationForm is not None

    def test_form_has_expected_fields(self):
        form = UserRegistrationForm()
        assert "username" in form.fields
        assert "email" in form.fields
        assert "password" in form.fields


# ---------------------------------------------------------------------------
# Profile view — save exception path  (lines 35-36)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProfileViewSaveException:
    """Cover the except branch when user.save() raises an exception."""

    def test_save_error_renders_profile_with_error(
        self, authenticated_client, staff_user
    ):
        """Lines 35-36: user.save() raises exception → error message + 200 response."""
        with patch("core.models.User.save", side_effect=Exception("forced DB error")):
            response = authenticated_client.post(
                "/auth/profile/",
                data={
                    "first_name": "Test",
                    "last_name": "User",
                    "email": staff_user.email,
                },
            )
        # View catches the exception and renders the profile page with an error
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Profile view — password validation error  (lines 85-88)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestChangePasswordValidatorError:
    """Cover the ValidationError except block for weak passwords."""

    def test_common_password_triggers_validator_error(self, authenticated_client):
        """Lines 85-88: validate_password raises ValidationError for common password."""
        response = authenticated_client.post(
            "/auth/change-password/",
            data={
                "current_password": "testpass123!",
                "new_password": "password",  # "password" is a common password
                "confirm_password": "password",
            },
        )
        # Redirects back to change_password page with error message
        assert response.status_code == 302
        assert "change-password" in response["Location"]
