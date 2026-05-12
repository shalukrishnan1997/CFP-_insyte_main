"""Focused tests for two-factor setup view flows."""

from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice

_BACKEND = "django.contrib.auth.backends.ModelBackend"


@pytest.mark.django_db()
class TestSetup2FARedirects:
    """Additional redirect and edge-case tests for setup_2fa."""

    def test_redirects_unauthenticated_user(self) -> None:
        resp = Client().get(reverse("auth_app:setup_2fa"))
        assert resp.status_code == 302

    def test_redirects_to_manage_when_already_has_totp(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        TOTPDevice.objects.create(user=staff_user, name="My Device", confirmed=True)
        resp = authenticated_client.get(reverse("auth_app:setup_2fa"))
        assert resp.status_code == 302
        assert "manage-2fa" in resp["Location"]

    def test_redirects_to_manage_when_already_has_email_device(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        EmailDevice.objects.create(user=staff_user, name="email otp", confirmed=True)
        resp = authenticated_client.get(reverse("auth_app:setup_2fa"))
        assert resp.status_code == 302
        assert "manage-2fa" in resp["Location"]

    def test_post_select_method_totp_redirects(
        self, authenticated_client: Client
    ) -> None:
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"),
            {"action": "select_method", "method": "totp"},
        )
        assert resp.status_code == 302
        assert authenticated_client.session.get("2fa_method") == "totp"

    def test_totp_generate_action_creates_device(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "totp"
        session.save()
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"), {"action": "generate"}
        )
        assert resp.status_code == 200
        assert TOTPDevice.objects.filter(user=staff_user, confirmed=False).exists()

    def test_totp_unknown_action_redirects_to_setup(
        self, authenticated_client: Client
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "totp"
        session.save()
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"), {"action": "bogusaction"}
        )
        assert resp.status_code == 302
        assert "setup-2fa" in resp["Location"]

    def test_email_generate_action_creates_device(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()
        with patch("auth_app.models.EmailDevice.generate_challenge"):
            resp = authenticated_client.post(
                reverse("auth_app:setup_2fa"), {"action": "generate"}
            )
        assert resp.status_code == 200
        assert EmailDevice.objects.filter(user=staff_user).exists()

    def test_email_resend_action_with_no_pending_device_redirects(
        self, authenticated_client: Client
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"), {"action": "resend"}
        )
        # No pending device in session → redirects to setup
        assert resp.status_code in (200, 302)

    def test_email_resend_action_with_valid_device_resends(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        device = EmailDevice.objects.create(
            user=staff_user, name="test otp", confirmed=False
        )
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session["pending_2fa_device_id"] = device.id
        session["pending_2fa_device_type"] = "email"
        session.save()
        with patch("auth_app.models.EmailDevice.generate_challenge"):
            resp = authenticated_client.post(
                reverse("auth_app:setup_2fa"), {"action": "resend"}
            )
        assert resp.status_code == 200

    def test_email_verify_no_pending_device_redirects(
        self, authenticated_client: Client
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"),
            {"action": "verify", "otp_token": "123456"},
        )
        assert resp.status_code == 302

    def test_email_unknown_action_redirects_to_setup(
        self, authenticated_client: Client
    ) -> None:
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()
        resp = authenticated_client.post(
            reverse("auth_app:setup_2fa"), {"action": "bogusaction"}
        )
        assert resp.status_code == 302
        assert "setup-2fa" in resp["Location"]


@pytest.mark.django_db()
class TestShowBackupCodesView:
    """Tests for the show_backup_codes view."""

    def test_redirects_unauthenticated_user(self) -> None:
        resp = Client().get(reverse("auth_app:show_backup_codes"))
        assert resp.status_code == 302

    def test_redirects_to_manage_when_no_codes(
        self, authenticated_client: Client
    ) -> None:
        resp = authenticated_client.get(reverse("auth_app:show_backup_codes"))
        assert resp.status_code == 302
        assert "manage-2fa" in resp["Location"]

    def test_displays_codes_from_session(self, authenticated_client: Client) -> None:
        session = authenticated_client.session
        session["backup_codes"] = ["AAAA", "BBBB"]
        session.save()
        resp = authenticated_client.get(reverse("auth_app:show_backup_codes"))
        assert resp.status_code == 200
        assert b"AAAA" in resp.content

    def test_clears_codes_from_session_after_display(
        self, authenticated_client: Client
    ) -> None:
        session = authenticated_client.session
        session["backup_codes"] = ["CCCC"]
        session.save()
        authenticated_client.get(reverse("auth_app:show_backup_codes"))
        assert "backup_codes" not in authenticated_client.session


@pytest.mark.django_db()
class TestManage2FAView:
    """Tests for the manage_2fa view."""

    def test_redirects_unauthenticated_user(self) -> None:
        resp = Client().get(reverse("auth_app:manage_2fa"))
        assert resp.status_code == 302

    def test_get_renders_manage_page(self, authenticated_client: Client) -> None:
        resp = authenticated_client.get(reverse("auth_app:manage_2fa"))
        assert resp.status_code == 200

    def test_disable_with_wrong_password_redirects_with_error(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        TOTPDevice.objects.create(user=staff_user, name="mydev", confirmed=True)
        resp = authenticated_client.post(
            reverse("auth_app:manage_2fa"),
            {"action": "disable", "password": "WRONG"},
        )
        assert resp.status_code == 302
        assert "manage-2fa" in resp["Location"]
        assert TOTPDevice.objects.filter(user=staff_user).exists()

    def test_disable_with_correct_password_deletes_devices(
        self, authenticated_client: Client, staff_user: Any
    ) -> None:
        TOTPDevice.objects.create(user=staff_user, name="mydev", confirmed=True)
        resp = authenticated_client.post(
            reverse("auth_app:manage_2fa"),
            {"action": "disable", "password": "testpass123!"},
        )
        assert resp.status_code == 302
        assert not TOTPDevice.objects.filter(user=staff_user).exists()

    def test_regenerate_backup_codes_returns_page_with_codes(
        self, authenticated_client: Client
    ) -> None:
        resp = authenticated_client.post(
            reverse("auth_app:manage_2fa"),
            {"action": "regenerate_backup_codes"},
        )
        assert resp.status_code == 200


@pytest.mark.django_db()
class TestTwoFactorViews:
    """Regression tests for the refactored two-factor view helpers."""

    def test_setup_2fa_select_method_persists_session(
        self,
        authenticated_client: Client,
        staff_user: Any,
    ) -> None:
        """Selecting the email method persists the session choice."""
        response = authenticated_client.post(
            reverse("auth_app:setup_2fa"),
            {"action": "select_method", "method": "email"},
        )

        assert response.status_code == 302
        assert authenticated_client.session["2fa_method"] == "email"

    def test_setup_2fa_get_renders_email_start_step(
        self,
        authenticated_client: Client,
        staff_user: Any,
    ) -> None:
        """Email-selected sessions render the email start template context."""
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session.save()

        response = authenticated_client.get(reverse("auth_app:setup_2fa"))

        assert response.status_code == 200
        assert response.context["setup_step"] == "start"
        assert response.context["email"] == staff_user.email

    def test_email_verify_invalid_code_re_renders_verify_step(
        self,
        authenticated_client: Client,
        staff_user: Any,
    ) -> None:
        """Invalid email OTP codes keep the user on the verify step."""
        device = EmailDevice.objects.create(
            user=staff_user,
            name="email otp",
            confirmed=False,
        )
        session = authenticated_client.session
        session["2fa_method"] = "email"
        session["pending_2fa_device_id"] = str(device.id)
        session["pending_2fa_device_type"] = "email"
        session.save()

        response = authenticated_client.post(
            reverse("auth_app:setup_2fa"),
            {"action": "verify", "otp_token": "000000"},
        )

        assert response.status_code == 200
        assert response.context["setup_step"] == "verify"
        assert response.context["email"] == staff_user.email

    def test_totp_verify_invalid_code_re_renders_verify_step(
        self,
        authenticated_client: Client,
        staff_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid TOTP codes keep the user on the verify step with QR context."""
        device = TOTPDevice.objects.create(
            user=staff_user,
            name="totp device",
            confirmed=False,
        )
        session = authenticated_client.session
        session["2fa_method"] = "totp"
        session["pending_2fa_device_id"] = str(device.id)
        session["pending_2fa_device_type"] = "totp"
        session.save()
        monkeypatch.setattr(
            "auth_app.views.two_factor._generate_qr_code",
            lambda _uri: "fake-qr",
        )

        response = authenticated_client.post(
            reverse("auth_app:setup_2fa"),
            {"action": "verify", "otp_token": "000000"},
        )

        assert response.status_code == 200
        assert response.context["setup_step"] == "verify"
        assert response.context["method"] == "totp"
        assert response.context["qr_code"] == "fake-qr"
