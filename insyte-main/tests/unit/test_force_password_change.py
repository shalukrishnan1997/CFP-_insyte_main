"""Tests for the forced first-login password change flow."""

from typing import Any

import pytest
from django.http import HttpResponseRedirect
from django.test import Client, RequestFactory
from django.urls import reverse

from auth_app.middleware import ForcePasswordChangeMiddleware
from tests.factories import UserFactory


def _dummy_get_response(request: Any) -> Any:
    from django.http import HttpResponse

    return HttpResponse("OK")


@pytest.mark.django_db()
class TestMustChangePasswordField:
    """The ``must_change_password`` field defaults to False on regular creates."""

    def test_default_is_false_for_factory_user(self) -> None:
        user = UserFactory()
        assert user.must_change_password is False


@pytest.mark.django_db()
class TestForcePasswordChangeMiddleware:
    """Unit tests for the redirect middleware."""

    def _request(self, rf: RequestFactory, user: Any, path: str = "/admin/") -> Any:
        request = rf.get(path)
        request.user = user
        request.session = {}  # type: ignore[assignment]
        from django.contrib.messages.storage.fallback import FallbackStorage

        request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
        return request

    def test_anonymous_user_passes_through(self, rf: RequestFactory) -> None:
        from django.contrib.auth.models import AnonymousUser

        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, AnonymousUser())
        assert middleware.process_request(request) is None

    def test_flag_false_passes_through(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True, must_change_password=False)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, user)
        assert middleware.process_request(request) is None

    def test_flag_true_not_2fa_verified_passes_through(
        self, rf: RequestFactory
    ) -> None:
        """Before 2FA verification, let ClientPortalMiddleware handle it."""
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: False  # type: ignore[method-assign]
        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, user)
        assert middleware.process_request(request) is None

    def test_flag_true_and_verified_redirects(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, user)
        response = middleware.process_request(request)
        assert isinstance(response, HttpResponseRedirect)
        assert response.url == reverse("auth_app:change_password")

    def test_exempt_url_change_password_passes_through(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, user, path=reverse("auth_app:change_password"))
        assert middleware.process_request(request) is None

    def test_exempt_url_logout_passes_through(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        middleware = ForcePasswordChangeMiddleware(_dummy_get_response)
        request = self._request(rf, user, path=reverse("auth_app:logout"))
        assert middleware.process_request(request) is None


@pytest.mark.django_db()
class TestChangePasswordClearsFlag:
    """The change_password view clears ``must_change_password`` on success."""

    def test_flag_cleared_on_successful_change(self) -> None:
        user = UserFactory(is_staff=True, must_change_password=True)
        user.set_password("OldStrong!xyz123")
        user.save()
        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("auth_app:change_password"),
            {
                "current_password": "OldStrong!xyz123",
                "new_password": "NewStronger!xyz456",
                "confirm_password": "NewStronger!xyz456",
            },
        )
        assert response.status_code == 302

        user.refresh_from_db()
        assert user.must_change_password is False
        assert user.check_password("NewStronger!xyz456")
