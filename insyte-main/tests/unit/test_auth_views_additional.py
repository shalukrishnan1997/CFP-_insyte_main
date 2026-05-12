"""Additional auth view tests for improved coverage.

Targets previously uncovered lines in auth_app/views/auth.py:
  47-71   - login_view when already authenticated
  97-98   - login POST with otp_token present
  121-130 - email-as-username fallback
  194-196 - TOTP device listing after valid credential check
  203-209 - email-only 2FA method selection
  335-354 - user_logout view
"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice

User = get_user_model()


# ---------------------------------------------------------------------------
# user_logout  (lines 335-354)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserLogout:
    """Cover user_logout view lines 335-354."""

    def test_post_authenticated_user_redirects_to_login(self, authenticated_client):
        response = authenticated_client.post("/auth/logout/")
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_get_logout_requires_post(self, authenticated_client):
        """logout is POST-only; GET returns 405."""
        response = authenticated_client.get("/auth/logout/")
        assert response.status_code == 405

    def test_unauthenticated_logout_redirects(self, client):
        """Unauthenticated user is redirected (login_required)."""
        response = client.post("/auth/logout/")
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# login_view — already-authenticated paths  (lines 47-71)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLoginViewAuthenticated:
    """Cover lines 47-71 in user_login: paths when user is already logged in."""

    def test_authenticated_no_2fa_device_redirects_to_setup(self, authenticated_client):
        """Lines 47-51, 62-68: no confirmed 2FA device → redirect to setup_2fa."""
        # authenticated_client uses staff_user which has no 2FA devices
        response = authenticated_client.get("/auth/login/")
        assert response.status_code == 302
        assert "setup" in response["Location"]

    def test_authenticated_has_device_but_not_session_verified(self, client, db):
        """Lines 47-51, 69-71: has device but not session-verified → logout + redirect."""
        user = User.objects.create_user(
            username="2fa_not_verified",
            email="2fa_notverified@test.local",
            password="somepass!123",
            is_staff=True,
        )
        TOTPDevice.objects.create(user=user, confirmed=True, name="mydevice")
        client.force_login(user)
        # No DEVICE_ID_SESSION_KEY → is_verified() returns False
        response = client.get("/auth/login/")
        assert response.status_code == 302
        # After logout, redirects to login
        assert "login" in response["Location"]

    def test_authenticated_is_verified_staff_redirects_to_dashboard(self, client, db):
        """Lines 53-55: is_verified=True + is_staff → admin dashboard redirect."""
        from django_otp import DEVICE_ID_SESSION_KEY

        user = User.objects.create_user(
            username="verifiedstaff_u",
            email="verifiedstaff@test.local",
            password="somepass!123",
            is_staff=True,
        )
        device = TOTPDevice.objects.create(user=user, confirmed=True, name="dev")
        client.force_login(user)
        # Mark user as OTP-verified in session
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        response = client.get("/auth/login/")
        assert response.status_code == 302
        loc = response["Location"]
        assert "admin" in loc or "dashboard" in loc

    def test_authenticated_is_verified_non_staff_no_roles_redirects_to_login(
        self, client, db
    ):
        """Verified user with no staff, client profile, groups, or perms → logout + login."""
        from django_otp import DEVICE_ID_SESSION_KEY

        user = User.objects.create_user(
            username="verifiedregular_u",
            email="verifiedregular@test.local",
            password="somepass!123",
            is_staff=False,
        )
        device = TOTPDevice.objects.create(user=user, confirmed=True, name="dev")
        client.force_login(user)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        response = client.get("/auth/login/")
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]
        follow = client.get("/auth/login/")
        assert follow.status_code == 200

    def test_authenticated_is_verified_non_staff_with_perm_redirects_to_dashboard(
        self, client, db
    ):
        """Non-staff with any direct permission may use staff dashboard."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        from django_otp import DEVICE_ID_SESSION_KEY

        user = User.objects.create_user(
            username="verified_perm_u",
            email="verifiedperm@test.local",
            password="somepass!123",
            is_staff=False,
        )
        ct = ContentType.objects.get_for_model(User)
        perm = Permission.objects.get(codename="add_user", content_type=ct)
        user.user_permissions.add(perm)
        device = TOTPDevice.objects.create(user=user, confirmed=True, name="dev")
        client.force_login(user)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        response = client.get("/auth/login/")
        assert response.status_code == 302
        assert "dashboard" in response["Location"]


# ---------------------------------------------------------------------------
# login_view POST paths  (lines 97-98, 121-130, 194-209)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLoginViewPostPaths:
    """Cover POST paths not yet tested in existing auth view tests."""

    def test_post_with_otp_token_but_no_session_redirects(self, client):
        """Lines 97-98: otp_token present → _handle_otp_verification → no session → redirect."""
        response = client.post(
            "/auth/login/", data={"otp_token": "123456", "username": ""}
        )
        assert response.status_code == 302

    def test_post_email_as_username_fallback_wrong_password(self, client, db):
        """Lines 121-130: email-format input → email lookup, wrong password → fails."""
        User.objects.create_user(
            username="gotauser",
            email="emaillogin@example.com",
            password="correctpass!",
        )
        response = client.post(
            "/auth/login/",
            data={"username": "emaillogin@example.com", "password": "wrongpass"},
        )
        # Auth fails → 200 with error
        assert response.status_code == 200

    def test_post_email_no_matching_user_returns_error(self, client):
        """Lines 121-130: email lookup returns no match → fails with error message."""
        response = client.post(
            "/auth/login/",
            data={"username": "nobody@nowhere.com", "password": "anypassword"},
        )
        assert response.status_code == 200

    def test_post_valid_user_with_totp_device_shows_otp_prompt(self, client, db):
        """Lines 162-196: valid credentials + confirmed TOTP device → show OTP form."""
        user = User.objects.create_user(
            username="totp_user",
            email="totp@test.local",
            password="validpass!123",
            is_active=True,
        )
        TOTPDevice.objects.create(user=user, confirmed=True, name="mytotp")
        response = client.post(
            "/auth/login/",
            data={"username": "totp_user", "password": "validpass!123"},
        )
        assert response.status_code == 200

    def test_post_valid_user_with_email_device_only_triggers_challenge(
        self, client, db
    ):
        """Lines 203-209: valid credentials + email device only → method=email + challenge."""
        user = User.objects.create_user(
            username="email2fa_user",
            email="e2fa@test.local",
            password="validpass!456",
            is_active=True,
        )
        EmailDevice.objects.create(user=user, confirmed=True, name="emaildev")
        with patch(
            "auth_app.models.EmailDevice.generate_challenge",
            return_value="654321",
        ):
            response = client.post(
                "/auth/login/",
                data={"username": "email2fa_user", "password": "validpass!456"},
            )
        assert response.status_code == 200
