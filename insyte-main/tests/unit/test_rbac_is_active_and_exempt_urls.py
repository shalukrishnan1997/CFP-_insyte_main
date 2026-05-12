"""Tests for the is_active re-check in RBAC helpers and EXEMPT_URLS hygiene.

Closes the audit-flagged gap where a session active when an admin deactivates
the account would keep being honored. Also pins the cleanup of two stale
URL-name aliases in ClientPortalMiddleware.EXEMPT_URLS.
"""

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory

from client_portal.middleware import ClientPortalMiddleware
from core.models import User
from responsehandling.permissions import (
    HasSystemAccessPermission,
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
    user_has_access,
)
from tests.factories import UserFactory


def _ok_view(request: Any, *args: Any, **kwargs: Any) -> HttpResponse:
    """Minimal view used as the protected target for decorator tests."""
    return HttpResponse("ok")


def _attach_session_and_messages(request: Any) -> None:
    """Equip a RequestFactory request with the middleware our helpers expect."""
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    request.session.save()
    MessageMiddleware(lambda r: HttpResponse()).process_request(request)


@pytest.mark.django_db()
class TestIsActiveReCheck:
    """Inactive users must be rejected even after a successful login."""

    def test_is_authenticated_and_is_staff_rejects_inactive_staff(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=True, is_active=False)
        request = rf.get("/admin/")
        request.user = user
        _attach_session_and_messages(request)

        wrapped = is_authenticated_and_is_staff(_ok_view)
        response = wrapped(request)

        assert response.status_code == 302
        assert response["Location"].startswith("/auth/login/")

    def test_is_authenticated_and_is_staff_rejects_inactive_group_user(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=False, is_active=False)
        user.groups.add(Group.objects.create(name="some-group"))
        request = rf.get("/admin/")
        request.user = user
        _attach_session_and_messages(request)

        wrapped = is_authenticated_and_is_staff(_ok_view)
        response = wrapped(request)

        assert response.status_code == 302
        assert response["Location"].startswith("/auth/login/")

    def test_has_permission_or_is_staff_rejects_inactive_staff(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=True, is_active=False)
        request = rf.get("/admin/")
        request.user = user
        _attach_session_and_messages(request)

        wrapped = has_permission_or_is_staff("view_donation")(_ok_view)
        response = wrapped(request)

        assert response.status_code == 302
        assert response["Location"].startswith("/auth/login/")

    def test_has_permission_or_is_staff_rejects_inactive_perm_holder(
        self, rf: RequestFactory
    ) -> None:
        user = UserFactory(is_staff=False, is_active=False)
        ct = ContentType.objects.get_for_model(User)
        perm, _ = Permission.objects.get_or_create(
            codename="view_donation",
            name="Can view donation",
            content_type=ct,
        )
        user.user_permissions.add(perm)
        request = rf.get("/admin/")
        request.user = user
        _attach_session_and_messages(request)

        wrapped = has_permission_or_is_staff("view_donation")(_ok_view)
        response = wrapped(request)

        assert response.status_code == 302
        assert response["Location"].startswith("/auth/login/")

    def test_user_has_access_returns_false_for_inactive_staff(self) -> None:
        user = UserFactory(is_staff=True, is_active=False)
        assert user_has_access(user) is False

    def test_user_has_access_returns_false_for_inactive_group_user(self) -> None:
        user = UserFactory(is_staff=False, is_active=False)
        user.groups.add(Group.objects.create(name="another-group"))
        assert user_has_access(user) is False

    def test_user_has_access_still_true_for_active_staff(self) -> None:
        user = UserFactory(is_staff=True, is_active=True)
        assert user_has_access(user) is True

    def test_drf_gate_rejects_inactive_user(self, rf: RequestFactory) -> None:
        user = UserFactory(is_staff=True, is_active=False)
        request = rf.get("/admin/api/donors/search/")
        request.user = user

        gate = HasSystemAccessPermission()
        assert gate.has_permission(request, view=object()) is False

    def test_drf_gate_rejects_anonymous(self, rf: RequestFactory) -> None:
        request = rf.get("/admin/api/donors/search/")
        request.user = AnonymousUser()

        gate = HasSystemAccessPermission()
        assert gate.has_permission(request, view=object()) is False


class TestExemptUrlsHygiene:
    """ClientPortalMiddleware.EXEMPT_URLS must only carry resolvable URL names."""

    def test_stale_password_change_aliases_removed(self) -> None:
        # These two strings are NOT URL names anywhere in the project (the
        # actual route is `change_password`). Listing them invited dead-code
        # rot and confused readers about which auth surfaces are 2FA-exempt.
        assert "password_change" not in ClientPortalMiddleware.EXEMPT_URLS
        assert "password_change_done" not in ClientPortalMiddleware.EXEMPT_URLS

    def test_change_password_still_exempt(self) -> None:
        # The real password-change endpoint must remain in the exempt set
        # so a user can rotate credentials without being bounced off by
        # the middleware (which is what enabled the stale aliases to live
        # there unnoticed in the first place).
        assert "change_password" in ClientPortalMiddleware.EXEMPT_URLS

    def test_password_reset_chain_still_exempt(self) -> None:
        for name in (
            "password_reset",
            "password_reset_done",
            "password_reset_confirm",
            "password_reset_complete",
        ):
            assert name in ClientPortalMiddleware.EXEMPT_URLS
