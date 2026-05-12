"""RBAC unit tests for ``responsehandling.permissions`` helpers.

Companion to ``test_permissions_extended.py``: focuses on edge cases the
existing suite does not exercise -- the DRF permission class, the
``non_staff_redirect`` parameter, inactive-but-grouped users, the
``is_superuser=True, is_staff=False`` corner, the
``request.user`` missing branch, and the cross-app suffix-match contract
that the four-phase model relocation depends on.
"""

from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from responsehandling.permissions import (
    HasSystemAccessPermission,
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
    user_has_access,
)
from tests.factories import UserFactory


def _make_request(rf: RequestFactory, user: Any, path: str = "/test/") -> HttpRequest:
    """Build a Django request with session/messages plumbing.

    Args:
        rf: Pytest ``RequestFactory`` fixture.
        user: User (or ``AnonymousUser``) to attach to the request.
        path: Request path; defaults to ``/test/`` so the agent_debug
            instrumentation in the dashboard branch stays quiet.

    Returns:
        A configured ``HttpRequest`` with ``request.user`` set.
    """
    request = rf.get(path)
    request.user = user
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def _dummy_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    """Trivial view returning 200 — used as the decorator's wrapped target."""
    return HttpResponse("OK")


def _reload(user: Any) -> Any:
    """Reload a User instance to clear Django's per-instance perm cache."""
    from core.models import User

    return User.objects.get(pk=user.pk)


@pytest.mark.django_db()
class TestIsAuthenticatedAndIsStaffEdgeCases:
    """Edge cases for ``@is_authenticated_and_is_staff`` not covered elsewhere."""

    def test_request_with_no_user_attribute_redirects(self, rf: RequestFactory) -> None:
        """The decorator uses ``getattr(request, "user", None)`` — verify the
        no-user branch redirects rather than raising AttributeError."""
        request = rf.get("/test/")
        request.session = {}  # type: ignore[assignment]
        request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
        # Deliberately do NOT set request.user.
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_anonymous_user_explicitly_redirects(self, rf: RequestFactory) -> None:
        request = _make_request(rf, AnonymousUser())
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_inactive_user_with_groups_is_rejected(
        self, rf: RequestFactory, db: None
    ) -> None:
        """``is_authenticated_and_is_staff`` re-checks ``is_active`` so a
        session that was active when an admin deactivated the account stops
        being honored on the next request. A user with groups but
        ``is_active=False`` is bounced to login.
        """
        del db
        user = UserFactory(is_staff=False, is_active=False)
        group = Group.objects.create(name="InactiveGroup")
        user.groups.add(group)

        request = _make_request(rf, _reload(user))
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_superuser_without_staff_flag_is_rejected(
        self, rf: RequestFactory, db: None
    ) -> None:
        """``is_superuser=True, is_staff=False`` with no groups/perms is
        rejected: the helper checks staff/groups/permissions, NOT the
        superuser flag. Django's ``User.has_perm`` would return True for
        superusers but the decorator never calls it.
        """
        del db
        user = UserFactory(is_staff=False, is_superuser=True)
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_non_staff_redirect_param_used_when_set(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Authenticated-but-no-access user should be sent to
        ``non_staff_redirect`` when provided."""
        del db
        user = UserFactory(is_staff=False)
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(
            _dummy_view, non_staff_redirect="/elsewhere/"
        )
        response = view(request)
        assert response.status_code == 302
        assert response["Location"] == "/elsewhere/"

    def test_no_access_default_redirect_is_login(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Without ``non_staff_redirect``, the helper falls back to
        ``reverse("auth_app:login")``."""
        del db
        user = UserFactory(is_staff=False)
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302
        assert response["Location"] == reverse("auth_app:login")

    def test_stale_group_cache_after_removal_blocks_access(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Removing the user's only group and reloading the instance must
        revoke access. The reload-after-mutation pattern is the contract
        callers rely on; this test pins it.
        """
        del db
        user = UserFactory(is_staff=False)
        group = Group.objects.create(name="EphemeralGroup")
        user.groups.add(group)

        view = is_authenticated_and_is_staff(_dummy_view)
        # First request: passes via group membership.
        request = _make_request(rf, _reload(user))
        assert view(request).status_code == 200

        # Drop the group; reload to clear the m2m cache on the instance.
        user.groups.remove(group)
        request = _make_request(rf, _reload(user))
        assert view(request).status_code == 302


@pytest.mark.django_db()
class TestHasPermissionOrIsStaffEdgeCases:
    """Edge cases for ``@has_permission_or_is_staff`` not covered elsewhere."""

    def test_active_superuser_bypasses_permission_check(
        self, rf: RequestFactory, db: None
    ) -> None:
        """An active superuser passes ``user.has_perm()`` for any string,
        even one that does not exist as a real permission. Documents the
        divergence from ``is_authenticated_and_is_staff``, which rejects
        the same user when ``is_staff`` is False.
        """
        del db
        user = UserFactory(is_staff=False, is_superuser=True)
        view = has_permission_or_is_staff("nonexistent.does_not_exist")(_dummy_view)
        request = _make_request(rf, user)
        response = view(request)
        assert response.status_code == 200

    def test_inactive_superuser_is_rejected(self, rf: RequestFactory, db: None) -> None:
        """``user.has_perm`` returns False for an inactive superuser, so the
        decorator must reject. Pins the ``is_active`` gate Django enforces
        in ``ModelBackend.has_perm``.
        """
        del db
        user = UserFactory(is_staff=False, is_superuser=True, is_active=False)
        view = has_permission_or_is_staff("nonexistent.does_not_exist")(_dummy_view)
        request = _make_request(rf, _reload(user))
        response = view(request)
        assert response.status_code == 302

    def test_request_with_no_user_attribute_redirects(self, rf: RequestFactory) -> None:
        """Mirrors the unauth branch in the dotted-perm form."""
        request = rf.get("/test/")
        request.session = {}  # type: ignore[assignment]
        request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
        view = has_permission_or_is_staff("core.view_user")(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_bare_codename_matches_other_app_label(
        self, rf: RequestFactory, db: None
    ) -> None:
        """The bare-codename branch suffix-matches against any installed
        app, which is the contract that survived the four-phase model
        relocation. Mock ``get_all_permissions`` to return a label the user
        physically does not have, and verify the suffix match still
        succeeds.
        """
        del db
        user = UserFactory(is_staff=False)
        request = _make_request(rf, user)

        with patch.object(
            type(user),
            "get_all_permissions",
            return_value={"some_other_app.view_donation"},
        ):
            view = has_permission_or_is_staff("view_donation")(_dummy_view)
            assert view(request).status_code == 200

    def test_bare_codename_rejects_partial_substring_match(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Suffix match means ``view_user`` must NOT match
        ``app.preview_user`` -- otherwise the contract leaks unintended
        access. This pins the ``.<codename>`` boundary.
        """
        del db
        user = UserFactory(is_staff=False)
        request = _make_request(rf, user)

        with patch.object(
            type(user),
            "get_all_permissions",
            return_value={"app.preview_user", "app.review_user"},
        ):
            view = has_permission_or_is_staff("view_user")(_dummy_view)
            assert view(request).status_code == 302

    def test_dotted_perm_uses_has_perm_not_suffix(
        self, rf: RequestFactory, db: None
    ) -> None:
        """When the codename contains a dot, the decorator calls
        ``user.has_perm`` directly. A user with ``otherapp.view_user``
        cached should NOT pass a check for ``targetapp.view_user``.
        """
        del db
        user = UserFactory(is_staff=False)
        request = _make_request(rf, user)

        with patch.object(type(user), "has_perm", return_value=False) as mock_has_perm:
            view = has_permission_or_is_staff("targetapp.view_user")(_dummy_view)
            response = view(request)
            assert response.status_code == 302
            mock_has_perm.assert_called_once_with("targetapp.view_user")


@pytest.mark.django_db()
class TestUserHasAccessEdgeCases:
    """Gaps left by ``test_permissions_extended.py::TestUserHasAccess``."""

    def test_inactive_user_with_groups_returns_false(self, db: None) -> None:
        """``user_has_access`` gates on ``is_active`` so a deactivated user
        loses system access immediately, even if they still hold groups.
        """
        del db
        user = UserFactory(is_staff=False, is_active=False)
        group = Group.objects.create(name="InactiveGroup")
        user.groups.add(group)
        assert user_has_access(_reload(user)) is False

    def test_active_superuser_without_staff_flag_returns_false(self, db: None) -> None:
        """``user_has_access`` only inspects ``is_staff`` / groups /
        explicit perms — it does NOT short-circuit on ``is_superuser``.
        """
        del db
        user = UserFactory(is_staff=False, is_superuser=True)
        assert user_has_access(user) is False

    def test_object_without_is_authenticated_returns_false(self) -> None:
        """A bare object with neither ``is_authenticated`` nor the User
        protocol must not raise — the helper short-circuits."""

        class NotAUser:
            is_authenticated = False

        assert user_has_access(NotAUser()) is False


@pytest.mark.django_db()
class TestHasSystemAccessPermission:
    """Full coverage for the DRF permission class — previously untested."""

    def _build(self, rf: RequestFactory, user: Any) -> HttpRequest:
        request = rf.get("/api/test/")
        request.user = user
        return request

    def test_anonymous_user_denied(self, rf: RequestFactory) -> None:
        request = self._build(rf, AnonymousUser())
        assert HasSystemAccessPermission().has_permission(request, view=None) is False

    def test_request_without_user_attribute_denied(self, rf: RequestFactory) -> None:
        """Class uses ``getattr(request, "user", None)`` — exercise the
        None branch."""
        request = rf.get("/api/test/")
        # Don't set request.user.
        assert HasSystemAccessPermission().has_permission(request, view=None) is False

    def test_staff_user_allowed(self, rf: RequestFactory, db: None) -> None:
        del db
        user = UserFactory(is_staff=True)
        request = self._build(rf, user)
        assert HasSystemAccessPermission().has_permission(request, view=None) is True

    def test_user_with_group_allowed(self, rf: RequestFactory, db: None) -> None:
        del db
        user = UserFactory(is_staff=False)
        group = Group.objects.create(name="ApiAccessGroup")
        user.groups.add(group)
        request = self._build(rf, _reload(user))
        assert HasSystemAccessPermission().has_permission(request, view=None) is True

    def test_user_with_explicit_permission_allowed(
        self, rf: RequestFactory, db: None
    ) -> None:
        del db
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(user)
        perm = Permission.objects.filter(content_type=ct, codename="view_user").first()
        assert perm is not None
        user.user_permissions.add(perm)
        request = self._build(rf, _reload(user))
        assert HasSystemAccessPermission().has_permission(request, view=None) is True

    def test_authenticated_user_without_access_denied(
        self, rf: RequestFactory, db: None
    ) -> None:
        del db
        user = UserFactory(is_staff=False)
        request = self._build(rf, user)
        assert HasSystemAccessPermission().has_permission(request, view=None) is False

    def test_active_superuser_without_staff_flag_denied(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Mirrors ``user_has_access`` — DRF class does not honor
        ``is_superuser`` implicitly. Pins the contract.
        """
        del db
        user = UserFactory(is_staff=False, is_superuser=True)
        request = self._build(rf, user)
        assert HasSystemAccessPermission().has_permission(request, view=None) is False
