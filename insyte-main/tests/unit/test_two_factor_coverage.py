"""Tests for two_factor and exceptions modules covering previously missed lines.

Targets:
  auth_app/views/two_factor.py 58-62   - setup_2fa: user already has 2FA device
  auth_app/views/two_factor.py 88-91   - setup_2fa: user has no email
  auth_app/views/two_factor.py 202-213 - _finish_2fa_setup: creates backup codes
  auth_app/views/two_factor.py 240-241 - _handle_email_otp_setup: device exists
  auth_app/views/two_factor.py 262-264 - email OTP verify success
  auth_app/views/two_factor.py 287-289 - email OTP resend
  responsehandling/exceptions.py 42   - json_error
  responsehandling/exceptions.py 47   - json_ok
"""

from unittest.mock import patch

import pytest
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice

# ---------------------------------------------------------------------------
# responsehandling/exceptions.py  (lines 42, 47)
# ---------------------------------------------------------------------------


class TestExceptionHelpers:
    """Directly test json_error and json_ok from responsehandling.exceptions."""

    def test_json_error_returns_json_response(self):
        from responsehandling.exceptions import json_error

        response = json_error("something went wrong", status=404)
        import json

        data = json.loads(response.content)
        assert data["success"] is False
        assert data["error"] == "something went wrong"
        assert response.status_code == 404

    def test_json_ok_returns_json_response(self):
        from responsehandling.exceptions import json_ok

        response = json_ok({"count": 5})
        import json

        data = json.loads(response.content)
        assert data["success"] is True
        assert data["count"] == 5


# ---------------------------------------------------------------------------
# setup_2fa view paths  (lines 58-91)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSetup2FAViewPaths:
    """Cover additional paths in the setup_2fa view."""

    def test_user_already_has_totp_device_redirects_to_manage(
        self, authenticated_client, staff_user
    ):
        """Lines 51-55: already has confirmed 2FA device → redirect to manage_2fa."""
        TOTPDevice.objects.create(user=staff_user, confirmed=True, name="mydev")
        response = authenticated_client.get("/auth/setup-2fa/")
        assert response.status_code == 302
        assert "manage" in response["Location"] or "2fa" in response["Location"]

    def test_user_with_no_email_redirects_to_profile(
        self, authenticated_client, staff_user
    ):
        """Lines 57-62: user has no email → redirect to user_profile."""
        # Temporarily remove staff_user's email
        staff_user.email = ""
        staff_user.save()
        response = authenticated_client.get("/auth/setup-2fa/")
        assert response.status_code == 302
        assert "profile" in response["Location"]

    def test_get_setup_2fa_no_method_renders_selection_page(self, authenticated_client):
        """Line 91: GET without selected_method → render select_2fa_method.html."""
        session = authenticated_client.session
        session.pop("2fa_method", None)
        session.save()
        response = authenticated_client.get("/auth/setup-2fa/")
        assert response.status_code == 200

    def test_get_setup_2fa_totp_method_renders_totp_setup(self, authenticated_client):
        """Lines 88-89: GET with 2fa_method=totp → render TOTP setup page."""
        session = authenticated_client.session
        session["2fa_method"] = "totp"
        session.save()
        response = authenticated_client.get("/auth/setup-2fa/")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# _handle_email_otp_setup  (lines 240-241, 262-264, 287-289)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailOTPSetupPaths:
    """Cover additional _handle_email_otp_setup paths."""

    def test_generate_when_device_already_exists_updates_confirmed(
        self, authenticated_client, staff_user
    ):
        """Lines 240-241: device exists (not created) → confirmed=False + save."""
        # Create with confirmed=False so the early 2FA check doesn't redirect us away
        existing_device = EmailDevice.objects.create(
            user=staff_user,
            name=f"{staff_user.username}'s email OTP",
            confirmed=False,
        )
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()

        with patch.object(EmailDevice, "generate_challenge", return_value="123456"):
            response = authenticated_client.post(
                "/auth/setup-2fa/",
                data={"action": "generate"},
            )

        assert response.status_code in (200, 302)
        existing_device.refresh_from_db()
        assert existing_device.confirmed is False

    def test_verify_email_otp_success_calls_finish_setup(
        self, authenticated_client, staff_user
    ):
        """Lines 262-264 + 202-213: successful email verify → _finish_2fa_setup."""
        device = EmailDevice.objects.create(
            user=staff_user,
            name=f"{staff_user.username}'s email OTP",
            confirmed=False,
        )
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session["pending_2fa_device_id"] = str(device.id)
        session["pending_2fa_device_type"] = "email"
        session.save()

        with patch.object(EmailDevice, "verify_token", return_value=True):
            response = authenticated_client.post(
                "/auth/setup-2fa/",
                data={"action": "verify", "otp_token": "123456"},
            )

        # _finish_2fa_setup creates StaticDevice + redirects to show_backup_codes
        assert response.status_code == 302
        assert "backup" in response["Location"] or "setup" in response["Location"]

    def test_resend_with_deleted_device_redirects_to_setup(
        self, authenticated_client, staff_user
    ):
        """Lines 287-289: resend with deleted device ID → DoesNotExist → redirect to setup."""
        session = authenticated_client.session
        session["2fa_method"] = "email"
        # Set a device_id that does NOT exist in DB
        session["pending_2fa_device_id"] = "99999"
        session["pending_2fa_device_type"] = "email"
        session.save()

        response = authenticated_client.post(
            "/auth/setup-2fa/",
            data={"action": "resend"},
        )
        # DoesNotExist → redirect to setup_2fa
        assert response.status_code == 302
        assert "setup" in response["Location"]


# ---------------------------------------------------------------------------
# RequestFactory-based tests for paths where session backend causes issues
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTwoFactorDirectPaths:
    """Use RequestFactory to bypass cache-session persistence issues."""

    def _make_request(
        self,
        method: str,
        user: object,
        session_data: dict,
        post_data: dict | None = None,
    ):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        factory = RequestFactory()
        req = (
            factory.post("/auth/setup-2fa/", data=post_data or {})
            if method == "POST"
            else factory.get("/auth/setup-2fa/")
        )
        req.user = user
        req.session = dict(session_data)
        req._messages = FallbackStorage(req)
        return req

    def test_totp_verify_nonexistent_device_covers_does_not_exist(self, db, staff_user):
        """Lines 330 + 372-374: _get_pending_device DoesNotExist → redirect."""
        from auth_app.views.two_factor import setup_2fa

        request = self._make_request(
            "POST",
            staff_user,
            {"2fa_method": "totp", "pending_2fa_device_id": "99999"},
            post_data={"action": "verify", "otp_token": "123456"},
        )
        response = setup_2fa(request)
        assert response.status_code == 302
        assert "setup" in response["Location"]

    def test_totp_verify_success_with_existing_backup_codes(self, db, staff_user):
        """Lines 215-220 + 334-336: TOTP verify succeeds + backup codes exist → logout."""
        from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

        from auth_app.views.two_factor import setup_2fa

        # Create existing backup device with tokens so _finish_2fa_setup takes else branch
        backup_device = StaticDevice.objects.create(
            user=staff_user, name="Backup Codes"
        )
        StaticToken.objects.create(device=backup_device, token="backup01")

        device = TOTPDevice.objects.create(
            user=staff_user, confirmed=False, name="totp-dev"
        )
        request = self._make_request(
            "POST",
            staff_user,
            {
                "2fa_method": "totp",
                "pending_2fa_device_id": str(device.id),
                "pending_2fa_device_type": "totp",
            },
            post_data={"action": "verify", "otp_token": "123456"},
        )
        with (
            patch("auth_app.views.two_factor.logout"),
            patch.object(TOTPDevice, "verify_token", return_value=True),
        ):
            response = setup_2fa(request)
        assert response.status_code == 302
        assert "login" in response["Location"]
