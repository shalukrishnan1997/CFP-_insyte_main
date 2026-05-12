"""RBAC integration tests for ClientPortalMiddleware + ForcePasswordChangeMiddleware.

The project's test settings strip ``ClientPortalMiddleware`` from the active
``MIDDLEWARE`` list so ``force_login`` works against view tests. These tests
follow the convention established in
``tests/integration/test_multitenancy_deep.py::TestPortalMiddlewareRouting`` —
exercise the middleware directly via ``RequestFactory`` while using real URL
resolution (so an exempt URL name truly resolves under ``/auth/``).

Coverage:

* Anonymous → ``/admin/*``: middleware passes; the view layer must redirect to
  login. Verified through the Django test client against the live URL config.
* Authenticated user without any 2FA device → redirect to ``setup_2fa``.
* Authenticated with a confirmed device but ``is_verified() = False`` →
  redirect to login with ``?next=``.
* Staff hitting ``/client/*`` → redirect to admin dashboard.
* Client portal user hitting ``/admin/*`` → redirect to client dashboard.
* Every URL name in ``EXEMPT_URLS`` resolves and is exempted.
* ``ForcePasswordChangeMiddleware`` defers to ``ClientPortalMiddleware`` — a
  user with both ``must_change_password=True`` and no 2FA device is sent to
  2FA setup first, not the password-change screen.
* Admin-like non-staff (group/permission) reaches ``/admin/*`` and is denied
  ``/client/*``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.middleware import ForcePasswordChangeMiddleware
from auth_app.models import EmailDevice
from client_portal import middleware as middleware_module
from client_portal.middleware import ClientPortalMiddleware
from clients.models import ClientPortalUser
from tests.factories import ClientFactory, UserFactory


def _ok_response(_request: HttpRequest) -> HttpResponse:
    """Trivial downstream callable for middleware construction."""
    return HttpResponse("ok")


def _bypass_2fa(monkeypatch: pytest.MonkeyPatch, user: Any) -> None:
    """Install stubs so the 2FA gate treats ``user`` as fully verified."""
    monkeypatch.setattr(
        middleware_module,
        "_has_confirmed_2fa_device",
        lambda _u: True,
    )
    monkeypatch.setattr(user, "is_verified", lambda: True, raising=False)


def _make_portal_user(client: Any) -> Any:
    """Create a non-staff user with a ``ClientPortalUser`` profile."""
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


@pytest.mark.django_db()
class TestRoleRouting:
    """Cross-routing branches when 2FA is satisfied."""

    def test_staff_hitting_client_path_redirects_to_admin_dashboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff = UserFactory(is_staff=True)
        _bypass_2fa(monkeypatch, staff)

        request = RequestFactory().get("/client/supporters/")
        request.user = staff

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url == reverse("custom_admin:admin_dashboard")

    def test_staff_hitting_admin_path_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff = UserFactory(is_staff=True)
        _bypass_2fa(monkeypatch, staff)

        request = RequestFactory().get("/admin/campaigns/")
        request.user = staff

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is None

    def test_portal_user_hitting_admin_path_redirects_to_client_dashboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        portal_user = _make_portal_user(ClientFactory())
        _bypass_2fa(monkeypatch, portal_user)

        request = RequestFactory().get("/admin/campaigns/")
        request.user = portal_user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url == reverse("client_portal:dashboard")

    def test_portal_user_hitting_client_path_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        portal_user = _make_portal_user(ClientFactory())
        _bypass_2fa(monkeypatch, portal_user)

        request = RequestFactory().get("/client/supporters/")
        request.user = portal_user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is None

    def test_admin_like_non_staff_user_can_reach_admin_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Group-backed non-staff users (no portal profile) reach /admin/*."""
        user = UserFactory(is_staff=False, is_superuser=False)
        user.groups.add(Group.objects.create(name="rbac-admin-like-access"))
        _bypass_2fa(monkeypatch, user)

        request = RequestFactory().get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is None

    def test_admin_like_non_staff_user_denied_on_client_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Group-backed non-staff users without a portal profile are bounced
        to login when they probe ``/client/*``.
        """
        user = UserFactory(is_staff=False, is_superuser=False)
        user.groups.add(Group.objects.create(name="rbac-no-portal-profile"))
        _bypass_2fa(monkeypatch, user)

        request = RequestFactory().get("/client/supporters/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url.startswith("/auth/login/")
        assert "next=/client/supporters/" in response.url

    def test_user_without_groups_or_portal_denied_on_admin_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare authenticated user (no staff flag, no group, no portal
        profile) hitting ``/admin/*`` must be redirected to login, never
        silently accepted.
        """
        user = UserFactory(is_staff=False, is_superuser=False)
        _bypass_2fa(monkeypatch, user)

        request = RequestFactory().get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url.startswith("/auth/login/")
        assert "next=/admin/campaigns/" in response.url


@pytest.mark.django_db()
class TestAnonymousRouting:
    """Anonymous traffic to gated paths must reach the login screen."""

    def test_anonymous_passes_middleware(self, rf: RequestFactory) -> None:
        """The middleware does not gate anonymous users itself — view-level
        decorators (``@is_authenticated_and_is_staff`` etc.) handle the redirect.
        Asserting middleware returns ``None`` documents that contract.
        """
        request = rf.get("/admin/campaigns/")
        request.user = AnonymousUser()
        response = ClientPortalMiddleware(_ok_response).process_request(request)
        assert response is None

    def test_anonymous_admin_request_redirects_to_login(self, client: Client) -> None:
        """End-to-end: hitting an admin URL anonymously must redirect to the
        login URL. The view-level ``@is_authenticated_and_is_staff`` decorator
        does the redirect; the middleware itself passes anonymous through.
        """
        response = client.get("/admin/campaigns/")
        assert response.status_code == 302
        assert "/auth/login/" in response.url


@pytest.mark.django_db()
class Test2FAEnforcement:
    """Mandatory 2FA gating against non-exempt URLs."""

    def test_no_device_redirects_to_setup_2fa(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True)
        request = rf.get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url == reverse("auth_app:setup_2fa")

    def test_confirmed_device_but_unverified_redirects_to_login(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=True)
        TOTPDevice.objects.create(user=user, name="rbac-totp", confirmed=True)
        user.is_verified = lambda: False  # type: ignore[method-assign]
        request = rf.get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url.startswith("/auth/login/")
        assert "next=/admin/campaigns/" in response.url

    def test_email_device_alone_satisfies_device_gate(self, rf: RequestFactory) -> None:
        """A confirmed EmailDevice (custom auth_app device) counts as 2FA."""
        user = UserFactory(is_staff=True)
        EmailDevice.objects.create(user=user, name="rbac-email", confirmed=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = rf.get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is None

    def test_static_backup_device_alone_satisfies_device_gate(
        self, rf: RequestFactory
    ) -> None:
        """A confirmed StaticDevice (backup codes) counts as 2FA."""
        user = UserFactory(is_staff=True)
        StaticDevice.objects.create(user=user, name="rbac-backup", confirmed=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = rf.get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is None

    def test_unconfirmed_device_does_not_count(self, rf: RequestFactory) -> None:
        """A device row with ``confirmed=False`` must not satisfy the 2FA gate."""
        user = UserFactory(is_staff=True)
        TOTPDevice.objects.create(user=user, name="rbac-pending", confirmed=False)
        request = rf.get("/admin/campaigns/")
        request.user = user

        response = ClientPortalMiddleware(_ok_response).process_request(request)

        assert response is not None
        assert response.status_code == 302
        assert response.url == reverse("auth_app:setup_2fa")


@pytest.mark.django_db()
class TestExemptUrls:
    """Every URL name in ``EXEMPT_URLS`` is genuinely exempted from 2FA gating.

    Stale names (Django contrib aliases that this project does not register)
    are tolerated — we only assert the middleware does not redirect for any
    URL that *does* resolve to a route mapped to that name.
    """

    # Map exempt URL names to their real /auth/-namespaced URLs. Any name not
    # listed here either (a) does not resolve in this URL config (legacy
    # contrib alias) or (b) needs path kwargs.
    AUTH_APP_PATHS: dict[str, str] = {
        "login": "/auth/login/",
        "logout": "/auth/logout/",
        "setup_2fa": "/auth/setup-2fa/",
        "manage_2fa": "/auth/manage-2fa/",
        "show_backup_codes": "/auth/backup-codes/",
        "user_profile": "/auth/profile/",
        "change_password": "/auth/change-password/",
        "password_reset": "/auth/password-reset/",
        "password_reset_done": "/auth/password-reset/done/",
        "password_reset_confirm": "/auth/password-reset-confirm/abc/xyz/",
        "password_reset_complete": "/auth/password-reset-complete/",
    }

    def test_every_exempt_name_is_either_resolvable_or_dead(self) -> None:
        """Each entry in ``EXEMPT_URLS`` is either mapped to a real auth_app
        path here, or is a known dead Django-contrib alias.
        """
        # Names left in EXEMPT_URLS that don't resolve in this project.
        # Documented so a future refactor can prune them safely.
        DEAD_NAMES = {"password_change", "password_change_done"}

        for name in ClientPortalMiddleware.EXEMPT_URLS:
            assert name in self.AUTH_APP_PATHS or name in DEAD_NAMES, (
                f"EXEMPT_URLS entry {name!r} is neither resolvable nor known-dead"
            )

    def test_user_without_2fa_can_reach_every_resolvable_exempt_url(
        self, rf: RequestFactory
    ) -> None:
        """An authenticated user with no 2FA device must NOT be bounced when
        hitting an exempt URL — that's how setup is reachable in the first
        place. Loop over every name and assert no redirect.
        """
        user = UserFactory(is_staff=True)
        user.is_verified = lambda: False  # type: ignore[method-assign]

        middleware = ClientPortalMiddleware(_ok_response)
        for name, path in self.AUTH_APP_PATHS.items():
            request = rf.get(path)
            request.user = user
            response = middleware.process_request(request)
            assert response is None, (
                f"Exempt URL {name!r} ({path}) was redirected: "
                f"{getattr(response, 'url', response)}"
            )


@pytest.mark.django_db()
class TestMiddlewareOrderingFor2FAVsPasswordChange:
    """``ForcePasswordChangeMiddleware`` runs after ``ClientPortalMiddleware``
    in the project's MIDDLEWARE list so 2FA setup wins over password change
    for a user who must do both. This guarantee is encoded by:
    1. ``ClientPortalMiddleware`` redirecting unverified users to setup_2fa
       regardless of ``must_change_password``.
    2. ``ForcePasswordChangeMiddleware`` returning ``None`` while the user is
       not yet 2FA-verified.
    """

    def test_user_with_both_flags_is_sent_to_2fa_first(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: False  # type: ignore[method-assign]

        request = rf.get("/admin/campaigns/")
        request.user = user

        cp_response = ClientPortalMiddleware(_ok_response).process_request(request)
        assert cp_response is not None
        assert cp_response.status_code == 302
        assert cp_response.url == reverse("auth_app:setup_2fa")

        # FallbackStorage avoids messages-framework AttributeError if the
        # password-change middleware ever attempts ``messages.info``.
        from django.contrib.messages.storage.fallback import FallbackStorage

        request.session = {}  # type: ignore[assignment]
        request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
        fp_response = ForcePasswordChangeMiddleware(_ok_response).process_request(
            request
        )
        assert fp_response is None, (
            "ForcePasswordChangeMiddleware fired before 2FA was satisfied — "
            "this would override the setup_2fa redirect."
        )

    def test_user_with_password_flag_after_2fa_is_redirected_to_change_password(
        self, rf: RequestFactory
    ) -> None:
        """Sanity check — once 2FA is satisfied, the password-change gate
        actually fires (so the order genuinely matters above)."""
        user = UserFactory(is_staff=True, must_change_password=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]

        from django.contrib.messages.storage.fallback import FallbackStorage

        request = rf.get("/admin/campaigns/")
        request.user = user
        request.session = {}  # type: ignore[assignment]
        request._messages = FallbackStorage(request)  # type: ignore[attr-defined]

        response = ForcePasswordChangeMiddleware(_ok_response).process_request(request)
        assert response is not None
        assert response.status_code == 302
        assert response.url == reverse("auth_app:change_password")


@pytest.mark.django_db()
class TestResolveFailureFallback:
    """Paths that don't resolve to any view (404) are passed through so
    Django's normal 404 handler runs.
    """

    def test_unresolved_path_passes_through(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True)
        request = rf.get("/this/path/does/not/exist/at/all/")
        request.user = user

        # When ``resolve()`` raises, the middleware short-circuits before
        # touching the 2FA gate — assert it was never even called.
        with patch("client_portal.middleware._has_confirmed_2fa_device") as gate:
            response = ClientPortalMiddleware(_ok_response).process_request(request)
            gate.assert_not_called()

        assert response is None
