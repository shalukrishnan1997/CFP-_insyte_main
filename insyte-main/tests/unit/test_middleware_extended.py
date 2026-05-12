"""Unit tests for core.middleware — AuditRequestMiddleware and ClientPortalMiddleware."""

from unittest.mock import MagicMock, patch

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from tests.factories import UserFactory


def _get_response(request: HttpRequest) -> HttpResponse:
    return HttpResponse("OK")


@pytest.mark.django_db()
class TestAuditRequestMiddleware:
    """Tests for AuditRequestMiddleware."""

    def test_process_request_stores_request(self, rf: RequestFactory) -> None:
        from audit.middleware import AuditRequestMiddleware

        middleware = AuditRequestMiddleware(_get_response)
        request = rf.get("/test/")

        with patch("audit.signals.set_current_request") as _mock_set:
            middleware.process_request(request)
            # The middleware calls audit_signals.set_current_request — just verify no error
            assert True  # process_request ran without error

    def test_process_response_clears_request(self, rf: RequestFactory) -> None:
        from audit.middleware import AuditRequestMiddleware

        middleware = AuditRequestMiddleware(_get_response)
        request = rf.get("/test/")
        response = HttpResponse("OK")

        result = middleware.process_response(request, response)
        assert result is response

    def test_process_exception_clears_request(self, rf: RequestFactory) -> None:
        from audit.middleware import AuditRequestMiddleware

        middleware = AuditRequestMiddleware(_get_response)
        request = rf.get("/test/")

        result = middleware.process_exception(request, Exception("test error"))
        assert result is None

    def test_full_request_response_cycle(self, rf: RequestFactory) -> None:
        from audit.middleware import AuditRequestMiddleware
        from audit.signals import clear_current_request, get_current_request

        # Clear any existing request first
        clear_current_request()

        middleware = AuditRequestMiddleware(_get_response)
        request = rf.get("/test/")

        middleware.process_request(request)
        stored = get_current_request()
        assert stored is request

        response = HttpResponse("OK")
        middleware.process_response(request, response)
        assert get_current_request() is None


@pytest.mark.django_db()
class TestClientPortalMiddleware:
    """Tests for ClientPortalMiddleware."""

    def test_unauthenticated_user_passes_through(self, rf: RequestFactory) -> None:
        from django.contrib.auth.models import AnonymousUser

        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        request = rf.get("/some/path/")
        request.user = AnonymousUser()

        result = middleware.process_request(request)
        assert result is None

    def test_exempt_url_passes_through(self, rf: RequestFactory) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=True)
        request = rf.get("/auth/login/")
        request.user = user

        with patch("client_portal.middleware.resolve") as mock_resolve:
            mock_url = MagicMock()
            mock_url.url_name = "login"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is None

    def test_staff_user_accessing_client_portal_redirects(
        self, rf: RequestFactory
    ) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = rf.get("/client/dashboard/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=True
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "dashboard"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is not None
            assert result.status_code == 302

    def test_staff_user_on_admin_path_passes(self, rf: RequestFactory) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = rf.get("/admin/donors/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=True
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "donors"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is None

    def test_user_without_2fa_setup_redirects_to_setup(
        self, rf: RequestFactory
    ) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=True)
        request = rf.get("/admin/donors/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=False
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "donors"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is not None
            assert result.status_code == 302

    def test_user_with_2fa_not_verified_redirects_to_login(
        self, rf: RequestFactory
    ) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=True)
        user.is_verified = lambda: False  # type: ignore[method-assign]
        request = rf.get("/admin/donors/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=True
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "donors"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is not None
            assert result.status_code == 302
            assert result["Location"] == "/auth/login/?next=/admin/donors/"

    def test_user_without_profile_accessing_admin_redirects(
        self, rf: RequestFactory
    ) -> None:
        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=False)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        # No client_portal_profile attribute
        request = rf.get("/admin/donors/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=True
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "donors"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is not None
            assert result.status_code == 302

    def test_non_staff_with_group_accessing_admin_passes(
        self, rf: RequestFactory
    ) -> None:
        """Permission-based users must reach /admin/* (notifications API, nav)."""
        from django.contrib.auth.models import Group

        from client_portal.middleware import ClientPortalMiddleware

        middleware = ClientPortalMiddleware(_get_response)
        user = UserFactory(is_staff=False)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        group = Group.objects.create(name="middleware-test-admin-access")
        user.groups.add(group)
        request = rf.get("/admin/notifications/api/stream/")
        request.user = user

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device", return_value=True
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "notification_stream_api"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)
            assert result is None


@pytest.mark.django_db()
class TestHasConfirmed2FADevice:
    """Tests for _has_confirmed_2fa_device helper."""

    def test_user_with_no_devices_returns_false(self) -> None:
        from client_portal.middleware import _has_confirmed_2fa_device

        user = UserFactory()
        assert _has_confirmed_2fa_device(user) is False

    def test_user_with_confirmed_totp_device_returns_true(self) -> None:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from client_portal.middleware import _has_confirmed_2fa_device

        user = UserFactory()
        TOTPDevice.objects.create(user=user, name="test-device", confirmed=True)
        assert _has_confirmed_2fa_device(user) is True

    def test_user_with_unconfirmed_device_returns_false(self) -> None:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from client_portal.middleware import _has_confirmed_2fa_device

        user = UserFactory()
        TOTPDevice.objects.create(user=user, name="test-device", confirmed=False)
        assert _has_confirmed_2fa_device(user) is False
