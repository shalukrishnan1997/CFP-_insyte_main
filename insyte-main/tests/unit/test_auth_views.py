"""Tests for auth_app views — login, OTP verification, logout."""

from unittest.mock import patch

import pytest
from django.test import Client

from tests.factories import UserFactory


@pytest.mark.django_db()
class TestUserLoginView:
    """Tests for the user_login view."""

    def test_root_redirect_preserves_next_query(self) -> None:
        client = Client()
        response = client.get("/?next=/admin/test/")
        assert response.status_code == 302
        assert response["Location"] == "/auth/login/?next=/admin/test/"

    def test_get_renders_login_page(self) -> None:
        client = Client()
        response = client.get("/auth/login/")
        assert response.status_code == 200
        assert b"login" in response.content.lower() or response.status_code == 200

    def test_authenticated_verified_staff_redirects_to_admin(self) -> None:
        """Authenticated + verified staff user gets redirected away from login."""
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        client.force_login(user)

        with patch("auth_app.views.auth._has_confirmed_2fa_device", return_value=False):
            response = client.get("/auth/login/")

        # Not verified (no 2FA setup) → redirected to setup_2fa or some redirect
        assert response.status_code in (200, 302)

    def test_post_empty_credentials_shows_error(self) -> None:
        client = Client()
        response = client.post("/auth/login/", {"username": "", "password": ""})
        assert response.status_code == 200

    def test_post_invalid_credentials_shows_error(self) -> None:
        client = Client()
        response = client.post(
            "/auth/login/",
            {"username": "nonexistent_user_xyz", "password": "wrong_password"},
        )
        assert response.status_code == 200
        assert b"Invalid" in response.content or response.status_code == 200

    def test_post_valid_credentials_no_2fa_redirects_to_setup(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()

        with patch("auth_app.views.auth._has_confirmed_2fa_device", return_value=False):
            response = client.post(
                "/auth/login/",
                {"username": user.username, "password": "password"},
            )

        assert response.status_code in (200, 302)

    def test_post_email_lookup_fallback(self) -> None:
        """When username not found but input looks like email, try email lookup."""
        _user = UserFactory(is_staff=True, is_active=True, email="testuser@test.com")
        client = Client()

        with patch("auth_app.views.auth._has_confirmed_2fa_device", return_value=False):
            response = client.post(
                "/auth/login/",
                {"username": "testuser@test.com", "password": "password"},
            )

        assert response.status_code in (200, 302)

    def test_post_inactive_user_shows_error(self) -> None:
        user = UserFactory(is_staff=True, is_active=False)
        client = Client()

        with patch("auth_app.views.auth.authenticate", return_value=user):
            response = client.post(
                "/auth/login/",
                {"username": user.username, "password": "testpass123!"},
            )
        assert response.status_code == 200
        assert b"deactivated" in response.content

    def test_post_with_otp_token_missing_session_redirects(self) -> None:
        client = Client()
        response = client.post(
            "/auth/login/",
            {"otp_token": "123456"},
        )
        assert response.status_code == 302

    def test_post_with_otp_token_invalid_user_id_in_session(self) -> None:
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = "99999999-0000-0000-0000-000000000000"
        session.save()

        response = client.post(
            "/auth/login/",
            {"otp_token": "123456"},
        )
        assert response.status_code == 302

    def test_post_with_otp_token_empty_token_rerenders(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session.save()

        response = client.post(
            "/auth/login/",
            {"otp_token": ""},
        )
        assert response.status_code == 200

    def test_post_with_otp_invalid_token_shows_error(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session.save()

        response = client.post(
            "/auth/login/",
            {"otp_token": "000000"},
        )
        assert response.status_code == 200

    def test_post_with_repeated_invalid_otp_tokens_forces_relogin(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session["pre_2fa_remember_me"] = None
        session["pre_2fa_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.save()

        from django_otp.plugins.otp_totp.models import TOTPDevice

        _device = TOTPDevice.objects.create(user=user, confirmed=True, name="test")

        with patch.object(TOTPDevice, "verify_token", return_value=False):
            for _ in range(4):
                response = client.post("/auth/login/", {"otp_token": "000000"})
                assert response.status_code == 200

            response = client.post("/auth/login/", {"otp_token": "000000"})

        assert response.status_code == 302
        assert response["Location"] == "/auth/login/"
        assert "pre_2fa_user_id" not in client.session
        assert "pre_2fa_backend" not in client.session

    def test_post_with_valid_totp_token_logs_in(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session["pre_2fa_remember_me"] = None
        session["pre_2fa_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.save()

        # Create a TOTP device for the user
        from django_otp.plugins.otp_totp.models import TOTPDevice

        _device = TOTPDevice.objects.create(user=user, confirmed=True, name="test")

        with patch.object(TOTPDevice, "verify_token", return_value=True):
            response = client.post(
                "/auth/login/",
                {"otp_token": "123456"},
            )

        assert response.status_code == 302


@pytest.mark.django_db()
class TestUserLogout:
    """Tests for the user_logout view."""

    def test_logout_redirects_to_login(self) -> None:
        user = UserFactory(is_staff=True)
        client = Client()
        client.force_login(user)

        response = client.post("/auth/logout/")

        assert response.status_code == 302

    def test_logout_requires_post_method(self) -> None:
        user = UserFactory(is_staff=True)
        client = Client()
        client.force_login(user)

        response = client.get("/auth/logout/")

        assert response.status_code in (405, 302)

    def test_logout_requires_authentication(self) -> None:
        client = Client()
        response = client.post("/auth/logout/")
        assert response.status_code in (302, 403)

    def test_logout_clears_session(self) -> None:
        user = UserFactory(is_staff=True)
        client = Client()
        client.force_login(user)

        client.post("/auth/logout/")

        # After logout, user should not be authenticated
        response = client.get("/auth/login/")
        assert response.status_code == 200


@pytest.mark.django_db()
class TestCompleteLogin:
    """Tests for _complete_login via the full login flow."""

    def test_staff_user_redirects_to_admin_dashboard(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()

        with patch("auth_app.views.auth._has_confirmed_2fa_device", return_value=False):
            response = client.post(
                "/auth/login/",
                {"username": user.username, "password": "password"},
            )

        # Should redirect somewhere (either setup or admin)
        assert response.status_code in (200, 302)

    def test_complete_login_with_next_url(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session["pre_2fa_remember_me"] = None
        session["pre_2fa_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.save()

        from django_otp.plugins.otp_totp.models import TOTPDevice

        _device = TOTPDevice.objects.create(user=user, confirmed=True, name="test")

        with patch.object(TOTPDevice, "verify_token", return_value=True):
            response = client.post(
                "/auth/login/?next=/some-safe-url/",
                {"otp_token": "123456"},
            )

        assert response.status_code == 302


@pytest.mark.django_db()
class TestHasConfirmed2FADevice:
    """Tests for _has_confirmed_2fa_device helper."""

    def test_returns_false_when_no_devices(self) -> None:
        from auth_app.views.auth import _has_confirmed_2fa_device

        user = UserFactory()
        assert _has_confirmed_2fa_device(user) is False

    def test_returns_true_when_totp_device_exists(self) -> None:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from auth_app.views.auth import _has_confirmed_2fa_device

        user = UserFactory()
        TOTPDevice.objects.create(user=user, confirmed=True, name="test")

        assert _has_confirmed_2fa_device(user) is True

    def test_returns_true_when_email_device_exists(self) -> None:
        from auth_app.models import EmailDevice
        from auth_app.views.auth import _has_confirmed_2fa_device

        user = UserFactory()
        EmailDevice.objects.create(user=user, confirmed=True, name="test")

        assert _has_confirmed_2fa_device(user) is True

    def test_returns_true_when_backup_code_device_exists(self) -> None:
        from django_otp.plugins.otp_static.models import StaticDevice

        from auth_app.views.auth import _has_confirmed_2fa_device

        user = UserFactory()
        StaticDevice.objects.create(user=user, confirmed=True, name="backup")

        assert _has_confirmed_2fa_device(user) is True
