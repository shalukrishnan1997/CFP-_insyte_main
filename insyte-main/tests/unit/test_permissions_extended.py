"""Unit tests for responsehandling.permissions decorators."""

from typing import Any

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from responsehandling.permissions import (
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
    user_has_access,
)
from tests.factories import UserFactory


def _make_request(rf: RequestFactory, user: Any, path: str = "/test/") -> HttpRequest:
    """Create a request with session/messages support."""
    request = rf.get(path)
    request.user = user
    # Needed for django.contrib.messages
    request.session = {}  # type: ignore[assignment]
    messages = FallbackStorage(request)
    request._messages = messages  # type: ignore[attr-defined]
    return request


def _dummy_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return HttpResponse("OK")


@pytest.mark.django_db()
class TestIsAuthenticatedAndIsStaff:
    """Tests for @is_authenticated_and_is_staff decorator."""

    def test_unauthenticated_redirects_to_login(self, rf: RequestFactory) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = _make_request(rf, AnonymousUser())
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_staff_user_passes_through(self, rf: RequestFactory, db: None) -> None:
        user = UserFactory(is_staff=True)
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 200

    def test_user_with_groups_passes_through(
        self, rf: RequestFactory, db: None
    ) -> None:
        from django.contrib.auth.models import Group

        user = UserFactory(is_staff=False)
        group = Group.objects.create(name="TestGroup")
        user.groups.add(group)
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 200

    def test_user_with_permissions_passes_through(
        self, rf: RequestFactory, db: None
    ) -> None:
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = UserFactory(is_staff=False)
        ct = ContentType.objects.first()
        perm = Permission.objects.filter(content_type=ct).first()
        if perm:
            user.user_permissions.add(perm)
            request = _make_request(rf, user)
            view = is_authenticated_and_is_staff(_dummy_view)
            response = view(request)
            assert response.status_code == 200

    def test_authenticated_no_access_redirects(
        self, rf: RequestFactory, db: None
    ) -> None:
        user = UserFactory(is_staff=False)
        # No groups, no permissions
        request = _make_request(rf, user)
        view = is_authenticated_and_is_staff(_dummy_view)
        response = view(request)
        assert response.status_code == 302

    def test_works_as_decorator_without_parentheses(
        self, rf: RequestFactory, db: None
    ) -> None:
        """@is_authenticated_and_is_staff applied directly to a function."""

        @is_authenticated_and_is_staff
        def my_view(req: HttpRequest) -> HttpResponse:
            return HttpResponse("OK")

        user = UserFactory(is_staff=True)
        request = _make_request(rf, user)
        response = my_view(request)
        assert response.status_code == 200

    def test_custom_login_url(self, rf: RequestFactory, db: None) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = _make_request(rf, AnonymousUser())
        view = is_authenticated_and_is_staff(_dummy_view, login_url="/custom-login/")
        response = view(request)
        assert response.status_code == 302
        assert "/custom-login/" in response["Location"]


@pytest.mark.django_db()
class TestHasPermissionOrIsStaff:
    """Tests for @has_permission_or_is_staff decorator."""

    def test_unauthenticated_redirects(self, rf: RequestFactory) -> None:
        from django.contrib.auth.models import AnonymousUser

        view = has_permission_or_is_staff("view_campaign")(_dummy_view)
        request = _make_request(rf, AnonymousUser())
        response = view(request)
        assert response.status_code == 302

    def test_staff_user_always_has_access(self, rf: RequestFactory, db: None) -> None:
        user = UserFactory(is_staff=True)
        view = has_permission_or_is_staff("view_campaign")(_dummy_view)
        request = _make_request(rf, user)
        response = view(request)
        assert response.status_code == 200

    def test_user_with_full_perm_string_passes(
        self, rf: RequestFactory, db: None
    ) -> None:
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(user)
        perm = Permission.objects.filter(content_type=ct, codename="view_user").first()
        if perm:
            user.user_permissions.add(perm)
            # Reload user to clear permission cache
            from core.models import User

            user = User.objects.get(pk=user.pk)
            view = has_permission_or_is_staff(f"{ct.app_label}.view_user")(_dummy_view)
            request = _make_request(rf, user)
            response = view(request)
            assert response.status_code == 200

    def test_user_without_perm_redirects(self, rf: RequestFactory, db: None) -> None:
        user = UserFactory(is_staff=False)
        view = has_permission_or_is_staff("nonexistent_permission")(_dummy_view)
        request = _make_request(rf, user)
        response = view(request)
        assert response.status_code == 302

    def test_short_permission_name_without_perm_redirects(
        self, rf: RequestFactory, db: None
    ) -> None:
        user = UserFactory(is_staff=False)
        view = has_permission_or_is_staff("view_campaign")(_dummy_view)
        request = _make_request(rf, user)
        response = view(request)
        assert response.status_code == 302

    def test_short_permission_name_matches_any_app(
        self, rf: RequestFactory, db: None
    ) -> None:
        """Bare codenames resolve regardless of the owning app's label.

        This is the contract that lets the decorator survive model moves
        between Django apps without rewriting every call site.
        """
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(user)
        perm = Permission.objects.filter(content_type=ct, codename="view_user").first()
        assert perm is not None, "view_user permission should exist for User model"
        user.user_permissions.add(perm)

        from core.models import User

        user = User.objects.get(pk=user.pk)  # reload to clear permission cache
        view = has_permission_or_is_staff("view_user")(_dummy_view)
        request = _make_request(rf, user)
        response = view(request)
        assert response.status_code == 200


@pytest.mark.django_db()
class TestUserHasAccess:
    """Tests for user_has_access helper function."""

    def test_returns_false_for_none(self) -> None:
        assert user_has_access(None) is False

    def test_returns_false_for_anonymous_user(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        assert user_has_access(AnonymousUser()) is False

    def test_returns_true_for_staff(self) -> None:
        user = UserFactory(is_staff=True)
        assert user_has_access(user) is True

    def test_returns_false_for_non_staff_no_groups(self) -> None:
        user = UserFactory(is_staff=False)
        assert user_has_access(user) is False

    def test_returns_true_for_user_with_groups(self) -> None:
        from django.contrib.auth.models import Group

        user = UserFactory(is_staff=False)
        group = Group.objects.create(name="AccessGroup")
        user.groups.add(group)
        assert user_has_access(user) is True

    def test_returns_true_for_user_with_permissions(self) -> None:
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = UserFactory(is_staff=False)
        ct = ContentType.objects.first()
        perm = Permission.objects.filter(content_type=ct).first()
        if perm:
            user.user_permissions.add(perm)
            from core.models import User

            user = User.objects.get(pk=user.pk)
            assert user_has_access(user) is True
