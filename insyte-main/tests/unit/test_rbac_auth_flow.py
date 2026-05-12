"""RBAC tests for the auth flow.

Covers:
    * Anonymous-only public routes (login, password-reset family).
    * Authenticated-only routes redirecting anonymous users to login.
    * ``EmailDevice.verify_token`` lifecycle: expiry boundary, single-use,
      max-attempts short-circuit.
    * Password reset token single-use after success.
    * django-axes lockout via ``@override_settings(AXES_ENABLED=True)`` —
      5 failures lock the account, ``AXES_RESET_ON_SUCCESS`` clears the
      counter on a successful login.
    * Failed-OTP overflow does not leak partial-auth session.

The test settings module disables ``AXES_ENABLED`` and strips the client
portal routing middleware. Tests in this file re-enable axes per-test and
exercise the unwrapped views directly via the Django test client.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from auth_app.models import EmailDevice
from tests.factories import UserFactory

# ---------------------------------------------------------------------------
# Anonymous-only public routes
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAnonymousAccessibleAuthRoutes:
    """Routes that must remain reachable without authentication."""

    @pytest.mark.parametrize(
        "url_name",
        [
            "auth_app:login",
            "auth_app:password_reset",
            "auth_app:password_reset_done",
            "auth_app:password_reset_complete",
        ],
    )
    def test_anonymous_get_returns_200(self, url_name: str) -> None:
        """Anonymous GET on a public auth route renders the page (no redirect)."""
        client = Client()
        response = client.get(reverse(url_name))
        assert response.status_code == 200, (
            f"{url_name} should be reachable anonymously, got {response.status_code}"
        )

    def test_anonymous_can_load_password_reset_confirm(self) -> None:
        """Password-reset-confirm renders for anonymous users with a real token.

        The view stores the token in the session and redirects to the
        ``set-password`` form, so the response is a 302 to the same URL with
        the magic ``set-password`` slug. Either 200 (renders form directly) or
        a same-prefix 302 is acceptable as long as the user is not bounced to
        ``/auth/login/``.
        """
        from django.contrib.auth.tokens import default_token_generator

        user = UserFactory(is_staff=True)
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        client = Client()
        response = client.get(
            reverse(
                "auth_app:password_reset_confirm",
                kwargs={"uidb64": uidb64, "token": token},
            )
        )
        # Django's PasswordResetConfirmView redirects to a "set-password" URL
        # the first time, then renders 200. Either way it must NOT redirect
        # to /auth/login/.
        assert response.status_code in (200, 302)
        if response.status_code == 302:
            assert "/auth/login/" not in response["Location"]


# ---------------------------------------------------------------------------
# Authenticated-only routes redirect anonymous to login
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAuthenticatedRoutesRequireLogin:
    """Routes guarded by ``@login_required`` redirect anonymous users."""

    @pytest.mark.parametrize(
        "url_name",
        [
            "auth_app:user_profile",
            "auth_app:setup_2fa",
            "auth_app:manage_2fa",
            "auth_app:show_backup_codes",
            "auth_app:change_password",
        ],
    )
    def test_anonymous_get_redirects_to_login(self, url_name: str) -> None:
        """Anonymous GET on an authenticated-only route redirects to login."""
        client = Client()
        response = client.get(reverse(url_name))
        assert response.status_code == 302
        assert "/auth/login/" in response["Location"]

    def test_anonymous_post_logout_redirects_to_login(self) -> None:
        """Anonymous POST to ``/auth/logout/`` redirects to login."""
        client = Client()
        response = client.post(reverse("auth_app:logout"))
        assert response.status_code == 302
        assert "/auth/login/" in response["Location"]


# ---------------------------------------------------------------------------
# EmailDevice.verify_token lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestEmailDeviceVerifyTokenLifecycle:
    """Cover ``verify_token`` short-circuits beyond what existing tests assert."""

    def test_expired_at_boundary_returns_false(self) -> None:
        """Token expires the instant ``valid_minutes`` elapse."""
        user = UserFactory()
        device = EmailDevice(user=user, name="email otp", valid_minutes=5)
        device.token = make_password("123456")
        # 1 second past the 5-minute window
        device.generated_at = timezone.now() - timedelta(minutes=5, seconds=1)
        device.failed_attempts = 0
        assert device.verify_token("123456") is False

    def test_token_is_single_use_after_success(self) -> None:
        """A successfully verified code cannot be replayed."""
        user = UserFactory()
        device = EmailDevice.objects.create(user=user, name="email otp", confirmed=True)
        code = device.generate_challenge()

        assert device.verify_token(code) is True
        # Reload from DB to confirm clearing was persisted, not just in-memory.
        device.refresh_from_db()
        assert device.token == ""
        assert device.generated_at is None
        # Replaying the same code must fail.
        assert device.verify_token(code) is False

    def test_verify_token_short_circuits_after_max_attempts(self) -> None:
        """The 4th call (post 3 failures) returns False without checking the code.

        Existing tests cover ``verify_is_allowed``; this exercises the line
        113-114 short-circuit inside ``verify_token`` itself even when the
        caller passes the *correct* code.
        """
        user = UserFactory()
        device = EmailDevice.objects.create(
            user=user, name="email otp", confirmed=True, max_attempts=3
        )
        code = device.generate_challenge()

        # Exhaust the attempt budget with wrong codes.
        for _ in range(3):
            assert device.verify_token("000000") is False
        device.refresh_from_db()
        assert device.failed_attempts == 3

        # Even the genuine code is rejected once the cap is hit.
        assert device.verify_token(code) is False


# ---------------------------------------------------------------------------
# Password reset token reuse
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestPasswordResetTokenReuse:
    """A consumed password reset token must be rejected on the second use."""

    def test_token_invalid_after_password_change(self) -> None:
        """After resetting, the same uid+token cannot reset the password again."""
        user = UserFactory(
            username="reset-user",
            email="reset-user@test.local",
            is_staff=True,
        )
        client = Client()

        # 1. Request a reset email.
        response = client.post(
            reverse("auth_app:password_reset"),
            {"email": user.email},
        )
        assert response.status_code == 302
        assert len(mail.outbox) == 1

        # 2. Extract the uidb64/token from the email body.
        body = str(mail.outbox[0].body)
        match = re.search(
            r"/auth/password-reset-confirm/(?P<uid>[^/]+)/(?P<token>[^/\s]+)/",
            body,
        )
        assert match is not None, f"Expected reset link in email body: {body}"
        uidb64 = match.group("uid")
        token = match.group("token")

        # 3. The confirm view stores the token in session under ``set-password``
        #    when GET-ed with a valid token. Visit it once to seed the session.
        confirm_url = reverse(
            "auth_app:password_reset_confirm",
            kwargs={"uidb64": uidb64, "token": token},
        )
        response = client.get(confirm_url)
        assert response.status_code in (200, 302)

        # Django's PasswordResetConfirmView accepts new passwords at a URL
        # where ``token`` is replaced with the literal "set-password" once the
        # session has been seeded.
        set_password_url = reverse(
            "auth_app:password_reset_confirm",
            kwargs={"uidb64": uidb64, "token": "set-password"},
        )
        new_password = "FreshPass!2026"
        response = client.post(
            set_password_url,
            {"new_password1": new_password, "new_password2": new_password},
        )
        assert response.status_code == 302
        # Confirm the password actually changed.
        user.refresh_from_db()
        assert user.check_password(new_password)

        # 4. A fresh client tries to use the same uid+token a second time.
        #    Token is bound to the password hash and last_login, so changing
        #    the password invalidates the token.
        replay_client = Client()
        response = replay_client.get(confirm_url)
        # Django renders an "invalid link" page (still 200) — the key signal
        # is that POSTing to set-password no longer succeeds.
        replay_response = replay_client.post(
            set_password_url,
            {"new_password1": "ReplayPass!9999", "new_password2": "ReplayPass!9999"},
        )
        # A successful reset is 302 → password_reset_complete. An invalid
        # token re-renders the form (200) without changing the password.
        user.refresh_from_db()
        assert user.check_password(new_password), (
            "Password must remain the post-reset value; token replay "
            "succeeded — this is a security bug."
        )
        assert replay_response.status_code in (200, 302)


# ---------------------------------------------------------------------------
# OTP overflow does not leak partial-auth session
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestOTPOverflowSessionState:
    """5 failed OTP submissions must clear pre-2FA session and not auth-login."""

    def test_max_otp_failures_does_not_login_user(self) -> None:
        """After hitting the OTP failure cap, the user is not authenticated."""
        from unittest.mock import patch

        from django_otp.plugins.otp_totp.models import TOTPDevice

        user = UserFactory(is_staff=True, is_active=True)
        TOTPDevice.objects.create(user=user, confirmed=True, name="totp")

        client = Client()
        session = client.session
        session["pre_2fa_user_id"] = str(user.pk)
        session["pre_2fa_remember_me"] = None
        session["pre_2fa_backend"] = "django.contrib.auth.backends.ModelBackend"
        session.save()

        with patch.object(TOTPDevice, "verify_token", return_value=False):
            for _ in range(5):
                client.post("/auth/login/", {"otp_token": "000000"})

        # Pre-2FA scratch space wiped.
        assert "pre_2fa_user_id" not in client.session
        assert "pre_2fa_backend" not in client.session
        # Crucially, the user was NEVER fully authenticated by Django.
        assert "_auth_user_id" not in client.session


# ---------------------------------------------------------------------------
# django-axes lockout
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAxesLockout:
    """Verify django-axes locks the account after 5 failed attempts.

    Test settings disable axes for the rest of the suite. Per-test override
    flips it on; axes reads the setting at call time, so the middleware and
    the auth backend both honour the override. Lockout state lives in
    axes' own DB tables and is rolled back after each test.
    """

    def _reset_axes(self) -> None:
        """Clear all axes attempt records — guards against intra-class bleed."""
        from axes.models import AccessAttempt, AccessLog

        AccessAttempt.objects.all().delete()
        AccessLog.objects.all().delete()

    @override_settings(AXES_ENABLED=True)
    def test_five_failures_lock_account(self) -> None:
        """5 wrong-password POSTs lock subsequent (correct) logins out."""
        self._reset_axes()
        from axes.models import AccessAttempt

        user = UserFactory(username="axes-victim", is_staff=True, is_active=True)
        user.set_password("CorrectHorse!Battery42")
        user.save(update_fields=["password"])

        client = Client()
        for _ in range(5):
            response = client.post(
                "/auth/login/",
                {"username": user.username, "password": "WRONG"},
            )
            # 200 / 302 = re-rendered or redirected on bad creds.
            # 403 / 429 = axes blocked the request once the cap is reached.
            assert response.status_code in (200, 302, 403, 429)
            assert "_auth_user_id" not in client.session

        # axes recorded attempts under (username, ip).
        assert AccessAttempt.objects.filter(username=user.username).exists()

        # 6th POST with the *correct* password must NOT authenticate the user.
        response = client.post(
            "/auth/login/",
            {"username": user.username, "password": "CorrectHorse!Battery42"},
        )
        assert "_auth_user_id" not in client.session, (
            "Correct password authenticated through an axes lockout — "
            "lockout is not enforced."
        )

    @override_settings(AXES_ENABLED=True)
    def test_reset_on_success_clears_counter(self) -> None:
        """A successful login before the cap clears the failure counter."""
        self._reset_axes()
        from axes.models import AccessAttempt

        user = UserFactory(username="axes-resets", is_staff=True, is_active=True)
        user.set_password("CorrectHorse!Battery42")
        user.save(update_fields=["password"])

        client = Client()
        # 4 failures (one short of the limit).
        for _ in range(4):
            client.post(
                "/auth/login/",
                {"username": user.username, "password": "WRONG"},
            )
        assert AccessAttempt.objects.filter(username=user.username).exists()

        # Successful credential check — the user has no 2FA device, so the
        # view starts the 2FA setup flow (status 302). That still counts as a
        # successful authenticate() from axes' perspective, which fires the
        # post-success signal that clears AccessAttempt.
        response = client.post(
            "/auth/login/",
            {"username": user.username, "password": "CorrectHorse!Battery42"},
        )
        assert response.status_code == 302
        assert "_auth_user_id" in client.session
        # AXES_RESET_ON_SUCCESS purges the counter.
        assert not AccessAttempt.objects.filter(username=user.username).exists()
