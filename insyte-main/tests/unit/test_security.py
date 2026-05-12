"""Unit tests for security features — OTP, rate limiting, health check, views.

Tests the critical security improvements to verify they work correctly.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient

from tests.factories import UserFactory


def _remove_all_otp_devices(user: Any) -> None:
    """Remove all OTP devices for deterministic authentication tests."""
    from auth_app.models import EmailDevice

    TOTPDevice.objects.devices_for_user(user).delete()
    EmailDevice.objects.devices_for_user(user).delete()
    StaticDevice.objects.filter(user=user).delete()


# ═══════════════════════════════════════════════════════════════
# Health Check Endpoint
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestHealthCheck:
    """Tests for the /health/ endpoint."""

    def test_health_check_returns_response(self, client: Client) -> None:
        """Health check returns valid JSON with check results."""
        response = client.get("/health/")
        # In test env, Celery isn't running so health check may return 503
        assert response.status_code in (200, 503)
        data = response.json()
        assert "healthy" in data
        assert "checks" in data
        assert "database" in data["checks"]
        assert data["checks"]["database"]["healthy"] is True

    def test_health_check_includes_cache(self, client: Client) -> None:
        """Health check verifies cache connectivity."""
        response = client.get("/health/")
        data = response.json()
        assert "cache" in data["checks"]
        assert data["checks"]["cache"]["healthy"] is True

    def test_health_check_no_auth_required(self, client: Client) -> None:
        """Health check is accessible without authentication."""
        response = client.get("/health/")
        # Should return JSON, not a login redirect (302)
        assert response.status_code in (200, 503)
        assert response["Content-Type"] == "application/json"

    def test_health_live_returns_200_when_db_ok(self, client: Client) -> None:
        """Liveness endpoint returns 200 when database is reachable."""
        response = client.get("/health/live/")
        assert response.status_code == 200
        assert response.json() == {"alive": True}
        assert response["Content-Type"] == "application/json"


# ═══════════════════════════════════════════════════════════════
# OTP Security
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestOTPSecurity:
    """Tests for Email OTP device security."""

    def test_otp_uses_secrets_module(self) -> None:
        """OTP generation uses cryptographically secure random numbers."""
        import inspect

        from auth_app.models import EmailDevice

        source = inspect.getsource(EmailDevice.generate_challenge)
        assert "secrets" in source
        assert "random.randint" not in source

    def test_otp_generates_six_digit_code(self) -> None:
        """Generated OTP is always 6 digits."""
        user = UserFactory()
        from auth_app.models import EmailDevice

        device = EmailDevice.objects.create(user=user, name="test", confirmed=True)
        code = device.generate_challenge()
        assert len(code) == 6
        assert code.isdigit()
        assert 100000 <= int(code) <= 999999

    def test_otp_verification_success(self) -> None:
        """Valid OTP code verifies successfully."""
        user = UserFactory()
        from auth_app.models import EmailDevice

        device = EmailDevice.objects.create(user=user, name="test", confirmed=True)
        code = device.generate_challenge()
        assert device.verify_token(code) is True

    def test_otp_verification_failure(self) -> None:
        """Invalid OTP code fails verification."""
        user = UserFactory()
        from auth_app.models import EmailDevice

        device = EmailDevice.objects.create(user=user, name="test", confirmed=True)
        device.generate_challenge()
        assert device.verify_token("000000") is False

    def test_otp_max_attempts(self) -> None:
        """OTP locks out after max failed attempts."""
        user = UserFactory()
        from auth_app.models import EmailDevice

        device = EmailDevice.objects.create(
            user=user, name="test", confirmed=True, max_attempts=3
        )
        device.generate_challenge()

        for _ in range(3):
            device.verify_token("000000")

        # After 3 failed attempts, even correct code should fail
        assert device.verify_is_allowed() is False


# ═══════════════════════════════════════════════════════════════
# Login View
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestLoginView:
    """Tests for the login view."""

    def test_login_page_loads(self, client: Client) -> None:
        """Login page returns 200."""
        response = client.get("/auth/login/")
        assert response.status_code == 200

    def test_login_invalid_credentials(self, client: Client) -> None:
        """Login with wrong password fails gracefully."""
        UserFactory(username="testlogin")
        response = client.post(
            "/auth/login/",
            {"username": "testlogin", "password": "wrongpassword"},
        )
        assert response.status_code == 200  # Re-renders form
        assert b"Invalid" in response.content or b"invalid" in response.content

    def test_login_empty_fields(self, client: Client) -> None:
        """Login with empty fields shows error."""
        response = client.post(
            "/auth/login/",
            {"username": "", "password": ""},
        )
        assert response.status_code == 200

    def test_login_without_2fa_device_redirects_to_setup(self, client: Client) -> None:
        """Valid login without 2FA devices must redirect to setup flow."""
        user = UserFactory(username="nofa-user", is_staff=False)
        user.set_password("testpass123!")
        user.save(update_fields=["password"])
        _remove_all_otp_devices(user)

        response = client.post(
            "/auth/login/",
            {"username": user.username, "password": "testpass123!"},
        )

        assert response.status_code == 302
        assert response.url.endswith("/auth/setup-2fa/")
        assert "_auth_user_id" in client.session

    def test_middleware_blocks_user_without_2fa(self, rf: RequestFactory) -> None:
        """Middleware redirects authenticated users without 2FA to setup."""
        from client_portal.middleware import ClientPortalMiddleware

        user = UserFactory(is_staff=True, is_superuser=True)
        _remove_all_otp_devices(user)
        request = rf.get("/admin/")
        request.user = user

        middleware = ClientPortalMiddleware(
            get_response=lambda _request: HttpResponse()
        )
        response = middleware.process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url.endswith("/auth/setup-2fa/")


@pytest.mark.django_db()
class TestAdminApiPermissions:
    """Tests for staff-only custom admin REST API permissions."""

    def test_admin_api_denies_non_staff(self) -> None:
        """Non-staff authenticated users should receive 403 on admin API."""
        client = APIClient()
        user = UserFactory(is_staff=False)
        client.force_authenticate(user=user)

        response = client.get("/admin/api/letter-batches/")

        assert response.status_code == 403

    def test_admin_api_allows_staff(self) -> None:
        """Staff users can access admin API endpoints."""
        client = APIClient()
        user = UserFactory(is_staff=True, is_superuser=True)
        client.force_authenticate(user=user)

        response = client.get("/admin/api/letter-batches/")

        assert response.status_code == 200

    def test_admin_api_allows_non_staff_user_with_group_access(self) -> None:
        """Group-backed system users can access admin API endpoints."""
        client = APIClient()
        user = UserFactory(is_staff=False, is_superuser=False)
        user.groups.add(Group.objects.create(name="Operations"))
        client.force_authenticate(user=user)

        response = client.get("/admin/api/letter-batches/")

        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════
# Campaign Status Bug Fix
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignStatusFix:
    """Tests verifying the status='live' bug is fixed."""

    def test_active_campaigns_filter(self) -> None:
        """Campaign.objects.filter(status=Campaign.STATUS_ACTIVE) returns active campaigns."""
        from campaigns.models import Campaign
        from tests.factories import CampaignFactory

        CampaignFactory(status="active")
        CampaignFactory(status="draft")
        CampaignFactory(status="closed")

        active = Campaign.objects.filter(status=Campaign.STATUS_ACTIVE)
        assert active.count() == 1
        first = active.first()
        assert first is not None
        assert first.status == "active"

    def test_status_live_alias(self) -> None:
        """STATUS_LIVE is an alias for STATUS_ACTIVE with the same DB value."""
        from campaigns.models import Campaign

        assert Campaign.STATUS_LIVE == Campaign.STATUS_ACTIVE
        assert Campaign.STATUS_LIVE == "active"
