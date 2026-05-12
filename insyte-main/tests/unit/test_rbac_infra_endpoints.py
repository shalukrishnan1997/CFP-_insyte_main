"""RBAC tests for infrastructure endpoints.

Covers ``/health/live/``, ``/health/``, ``/metrics``, ``protected_media_serve``,
the audit log admin view, and the notification admin endpoints. Verifies that
each surface enforces the access posture documented in ``CLAUDE.md`` —
specifically that the liveness probe touches only the database, the metrics
endpoint enforces an IP allowlist, the protected media view rejects users
without system access, and the notification mark-read endpoint cannot be used
to mutate another user's record (no IDOR).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.http import Http404
from django.test import Client, RequestFactory
from django.test.utils import override_settings
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from tests.factories import UserFactory


def _login_with_2fa(client: Client, user: Any) -> None:
    """Force-login ``user`` and mark the session as 2FA-verified.

    Required when a test exercises ``ClientPortalMiddleware``: the middleware
    short-circuits to the 2FA setup/verify flow before evaluating the role
    gate, so unverified sessions never reach the view under test.
    """
    device = TOTPDevice.objects.create(user=user, confirmed=True, name="test-device")
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()


def _run_middleware_for_client_portal_user(rf: RequestFactory, path: str) -> Any:
    """Run ``ClientPortalMiddleware`` against a client-portal user hitting ``path``.

    The middleware is intentionally stripped from ``MIDDLEWARE`` in test
    settings (so direct view tests can run without role-routing redirects),
    so we instantiate it manually for the few tests that *do* need to verify
    the role gate.
    """
    from django.http import HttpResponse

    from client_portal.middleware import ClientPortalMiddleware
    from clients.models import ClientPortalUser
    from tests.factories import ClientFactory

    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=ClientFactory(),
        user=user,
        role="viewer",
        is_active=True,
    )
    TOTPDevice.objects.create(user=user, confirmed=True, name="t")

    request = rf.get(path)
    request.user = user
    # ``ClientPortalMiddleware`` calls ``user.is_verified()`` (added by
    # django-otp's middleware in real requests). Stub to True so the role
    # gate runs instead of the 2FA gate.
    user.is_verified = lambda: True  # type: ignore[method-assign]

    middleware = ClientPortalMiddleware(get_response=lambda _r: HttpResponse())
    return middleware.process_request(request)


# ---------------------------------------------------------------------------
# /health/live/ — liveness probe must only touch the DB
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestHealthLiveAnonymousAccess:
    """``/health/live/`` is a public liveness probe."""

    def test_anonymous_request_returns_200(self, client: Client) -> None:
        response = client.get("/health/live/")
        assert response.status_code == 200
        assert response.json() == {"alive": True}

    def test_anonymous_does_not_redirect_to_login(self, client: Client) -> None:
        """Liveness must never 302 — load balancers treat that as healthy."""
        response = client.get("/health/live/")
        assert response.status_code != 302


@pytest.mark.django_db()
class TestHealthLiveSideEffects:
    """``/health/live/`` must touch only the database — no cache/Celery/etc."""

    def test_does_not_touch_cache_celery_or_document_ai(
        self, rf: RequestFactory
    ) -> None:
        """Liveness must not call cache, Celery inspect, or Client lookups.

        A misconfigured probe that pings Celery would drain backends every
        time the worker pool blips, so this regression test pins the
        DB-only guarantee documented in ``CLAUDE.md``.
        """
        from core.health import health_live

        with (
            patch("django.core.cache.cache") as mock_cache,
            patch("responsehandling.celery.app") as mock_celery,
            patch("clients.models.Client") as mock_client_model,
        ):
            response = health_live(rf.get("/health/live/"))

        assert response.status_code == 200
        assert mock_cache.set.called is False
        assert mock_cache.get.called is False
        assert mock_celery.control.inspect.called is False
        assert mock_celery.connection_for_read.called is False
        assert mock_client_model.objects.filter.called is False

    def test_db_failure_returns_503(self, rf: RequestFactory) -> None:
        import json

        from core.health import health_live

        with patch("core.health.connection") as mock_conn:
            mock_conn.cursor.side_effect = Exception("db down")
            response = health_live(rf.get("/health/live/"))

        assert response.status_code == 503
        assert json.loads(response.content) == {
            "alive": False,
            "error": "database_unavailable",
        }


# ---------------------------------------------------------------------------
# /health/ — readiness probe DOES touch cache + Celery
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestHealthReadinessSideEffects:
    """``/health/`` is the full readiness probe — should call cache + Celery."""

    def test_anonymous_request_returns_response(self, client: Client) -> None:
        response = client.get("/health/")
        assert response.status_code in (200, 503)
        assert response["Content-Type"] == "application/json"

    def test_calls_cache_and_celery_inspect(self, rf: RequestFactory) -> None:
        from core.health import health_check

        with (
            patch("django.core.cache.cache") as mock_cache,
            patch("responsehandling.celery.app") as mock_celery,
        ):
            mock_cache.get.return_value = "ok"
            mock_celery.control.inspect.return_value.stats.return_value = {"w1": {}}
            health_check(rf.get("/health/"))

        assert mock_cache.set.called is True
        assert mock_celery.control.inspect.called is True


# ---------------------------------------------------------------------------
# /metrics — IP allowlist enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestMetricsIPAllowlist:
    """The Prometheus metrics endpoint must reject non-allowlisted IPs."""

    @override_settings(METRICS_ALLOWED_IPS=["10.0.0.1"], METRICS_TRUSTED_PROXIES=[])
    def test_unallowed_ip_returns_403(self, client: Client) -> None:
        response = client.get("/metrics", REMOTE_ADDR="8.8.8.8")
        assert response.status_code == 403

    @override_settings(METRICS_ALLOWED_IPS=["10.0.0.1"], METRICS_TRUSTED_PROXIES=[])
    def test_allowlisted_ip_returns_200(self, client: Client) -> None:
        response = client.get("/metrics", REMOTE_ADDR="10.0.0.1")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")

    @override_settings(METRICS_ALLOWED_IPS=["10.0.0.1"], METRICS_TRUSTED_PROXIES=[])
    def test_spoofed_xff_from_untrusted_peer_is_ignored(self, client: Client) -> None:
        """``X-Forwarded-For`` is honoured only from trusted proxies."""
        response = client.get(
            "/metrics",
            REMOTE_ADDR="8.8.8.8",
            HTTP_X_FORWARDED_FOR="10.0.0.1",
        )
        assert response.status_code == 403

    @override_settings(
        METRICS_ALLOWED_IPS=["10.0.0.1"],
        METRICS_TRUSTED_PROXIES=["172.16.0.1"],
    )
    def test_xff_from_trusted_proxy_is_used(self, client: Client) -> None:
        response = client.get(
            "/metrics",
            REMOTE_ADDR="172.16.0.1",
            HTTP_X_FORWARDED_FOR="10.0.0.1",
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /media/* — protected media serve
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestProtectedMediaServe:
    """``protected_media_serve`` enforces auth + system-access."""

    def test_anonymous_redirects_to_login(self, rf: RequestFactory) -> None:
        from responsehandling.urls import protected_media_serve

        request = rf.get("/media/uploads/x.csv")
        request.user = AnonymousUser()
        response = protected_media_serve(request, "uploads/x.csv")

        assert response.status_code == 302
        assert "/auth/login/" in response["Location"]

    def test_authenticated_no_access_user_404(self, rf: RequestFactory) -> None:
        """User without staff, groups, or permissions is denied via 404."""
        from responsehandling.urls import protected_media_serve

        user = UserFactory(is_staff=False)
        request = rf.get("/media/uploads/x.csv")
        request.user = user

        with pytest.raises(Http404):
            protected_media_serve(request, "uploads/x.csv")

    def test_staff_user_allowed(self, rf: RequestFactory, tmp_path: Path) -> None:
        from responsehandling.urls import protected_media_serve

        media_path = tmp_path / "uploads" / "report.csv"
        media_path.parent.mkdir(parents=True)
        media_path.write_text("contents")

        request = rf.get("/media/uploads/report.csv")
        request.user = UserFactory(is_staff=True)

        with override_settings(MEDIA_ROOT=tmp_path):
            response = protected_media_serve(request, "uploads/report.csv")

        assert response.status_code == 200

    def test_group_backed_user_allowed(
        self, rf: RequestFactory, tmp_path: Path
    ) -> None:
        """Non-staff users with a group still satisfy ``user_has_access``."""
        from responsehandling.urls import protected_media_serve

        media_path = tmp_path / "uploads" / "report.csv"
        media_path.parent.mkdir(parents=True)
        media_path.write_text("contents")

        user = UserFactory(is_staff=False, is_superuser=False)
        user.groups.add(Group.objects.create(name="Operations"))

        request = rf.get("/media/uploads/report.csv")
        request.user = user

        with override_settings(MEDIA_ROOT=tmp_path):
            response = protected_media_serve(request, "uploads/report.csv")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /admin/audit-history/ — audit log admin view
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAuditLogHistoryAccess:
    """``audit_log_history`` allows staff or holders of ``view_auditlog``."""

    def test_anonymous_redirects(self, client: Client) -> None:
        response = client.get("/admin/audit-history/")
        assert response.status_code == 302

    def test_staff_user_returns_200(self, client: Client) -> None:
        user = UserFactory(is_staff=True, is_superuser=True)
        _login_with_2fa(client, user)

        response = client.get("/admin/audit-history/")
        assert response.status_code == 200

    def test_user_with_view_auditlog_permission_returns_200(
        self, client: Client
    ) -> None:
        """Bare-codename permission match (model can move between apps)."""
        user = UserFactory(is_staff=False)
        permission = Permission.objects.get(
            codename="view_auditlog",
            content_type__app_label="audit",
        )
        user.user_permissions.add(permission)
        _login_with_2fa(client, user)

        response = client.get("/admin/audit-history/")
        assert response.status_code == 200

    def test_user_without_permission_or_staff_redirects(self, client: Client) -> None:
        """Group membership grants admin access but not the audit page."""
        user = UserFactory(is_staff=False)
        # Give the user a group so middleware lets them into ``/admin/``;
        # the per-view ``has_permission_or_is_staff`` check is what should reject.
        user.groups.add(Group.objects.create(name="Operations"))
        _login_with_2fa(client, user)

        response = client.get("/admin/audit-history/")
        assert response.status_code == 302

    def test_client_portal_user_blocked_by_middleware(self, rf: RequestFactory) -> None:
        response = _run_middleware_for_client_portal_user(rf, "/admin/audit-history/")
        assert response is not None
        assert response.status_code == 302
        assert response["Location"].startswith("/client/")


# ---------------------------------------------------------------------------
# /admin/notifications/* — notification admin endpoints
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestNotificationListAccess:
    """``notification_list`` is staff-gated and per-user scoped."""

    def test_anonymous_redirects(self, client: Client) -> None:
        response = client.get("/admin/notifications/")
        assert response.status_code == 302

    def test_client_portal_user_blocked_by_middleware(self, rf: RequestFactory) -> None:
        response = _run_middleware_for_client_portal_user(rf, "/admin/notifications/")
        assert response is not None
        assert response.status_code == 302
        assert response["Location"].startswith("/client/")

    def test_staff_only_sees_own_notifications(self, client: Client) -> None:
        from notifications.models import Notification

        user_a = UserFactory(is_staff=True)
        user_b = UserFactory(is_staff=True)

        Notification.objects.create(
            user=user_a,
            title="A1",
            message="for A",
            notification_type=Notification.TYPE_INFO,
        )
        Notification.objects.create(
            user=user_b,
            title="B1",
            message="for B",
            notification_type=Notification.TYPE_INFO,
        )

        _login_with_2fa(client, user_a)
        response = client.get("/admin/notifications/?format=json")

        assert response.status_code == 200
        data = response.json()
        titles = {n["title"] for n in data["notifications"]}
        assert titles == {"A1"}
        assert data["total_count"] == 1


@pytest.mark.django_db()
class TestNotificationMarkReadIDOR:
    """Cross-user mark-read attempts must 404 — not 200, not 500."""

    def test_anonymous_redirects(self, client: Client) -> None:
        from notifications.models import Notification

        target_user = UserFactory(is_staff=True)
        notif = Notification.objects.create(
            user=target_user,
            title="x",
            message="x",
            notification_type=Notification.TYPE_INFO,
        )
        response = client.post(f"/admin/notifications/{notif.id}/mark-read/")
        assert response.status_code == 302

    def test_user_a_cannot_mark_user_b_notification_read(self, client: Client) -> None:
        from notifications.models import Notification

        user_a = UserFactory(is_staff=True)
        user_b = UserFactory(is_staff=True)

        notif_b = Notification.objects.create(
            user=user_b,
            title="for B",
            message="msg",
            notification_type=Notification.TYPE_INFO,
            is_read=False,
        )

        _login_with_2fa(client, user_a)
        response = client.post(f"/admin/notifications/{notif_b.id}/mark-read/")

        assert response.status_code == 404

        notif_b.refresh_from_db()
        assert notif_b.is_read is False
        assert notif_b.read_at is None

    def test_user_can_mark_own_notification_read(self, client: Client) -> None:
        from notifications.models import Notification

        user = UserFactory(is_staff=True)
        notif = Notification.objects.create(
            user=user,
            title="own",
            message="msg",
            notification_type=Notification.TYPE_INFO,
            is_read=False,
        )

        _login_with_2fa(client, user)
        response = client.post(f"/admin/notifications/{notif.id}/mark-read/")

        assert response.status_code == 200
        notif.refresh_from_db()
        assert notif.is_read is True
        assert notif.read_at is not None


@pytest.mark.django_db()
class TestNotificationCountAndMarkAll:
    """``notification_count_api`` and ``mark_all_read`` are per-user."""

    def test_count_api_excludes_other_users(self, client: Client) -> None:
        from notifications.models import Notification

        user_a = UserFactory(is_staff=True)
        user_b = UserFactory(is_staff=True)

        Notification.objects.create(
            user=user_a,
            title="A",
            message="m",
            notification_type=Notification.TYPE_INFO,
            is_read=False,
        )
        for i in range(3):
            Notification.objects.create(
                user=user_b,
                title=f"B{i}",
                message="m",
                notification_type=Notification.TYPE_INFO,
                is_read=False,
            )

        _login_with_2fa(client, user_a)
        response = client.get("/admin/notifications/api/count/")

        assert response.status_code == 200
        assert response.json() == {"unread_count": 1}

    def test_mark_all_read_only_affects_caller(self, client: Client) -> None:
        from notifications.models import Notification

        user_a = UserFactory(is_staff=True)
        user_b = UserFactory(is_staff=True)

        notif_a = Notification.objects.create(
            user=user_a,
            title="A",
            message="m",
            notification_type=Notification.TYPE_INFO,
            is_read=False,
        )
        notif_b = Notification.objects.create(
            user=user_b,
            title="B",
            message="m",
            notification_type=Notification.TYPE_INFO,
            is_read=False,
        )

        _login_with_2fa(client, user_a)
        response = client.post("/admin/notifications/mark-all-read/")

        assert response.status_code == 200
        assert response.json()["count"] == 1

        notif_a.refresh_from_db()
        notif_b.refresh_from_db()
        assert notif_a.is_read is True
        assert notif_b.is_read is False
