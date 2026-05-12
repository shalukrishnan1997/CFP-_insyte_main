"""Tests for responsehandling context processors.

Targets previously uncovered lines:
  44-45  - user_permissions: staff user path
  50-61  - user_permissions: non-staff user with groups/permissions
"""

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.http import HttpRequest

from responsehandling.context_processors import currency_constants, user_permissions
from tests.factories import UserFactory


class TestCurrencyConstants:
    """Tests for currency_constants context processor."""

    def test_returns_currency_symbol_and_code(self):
        request = HttpRequest()
        result = currency_constants(request)
        assert "currency_symbol" in result
        assert "currency_code" in result
        assert result["currency_symbol"]
        assert result["currency_code"]


class TestUserPermissionsContextProcessor:
    """Tests for user_permissions context processor."""

    def test_unauthenticated_user_returns_empty_defaults(self):
        request = HttpRequest()
        request.user = AnonymousUser()
        result = user_permissions(request)
        assert result["is_admin_user"] is False
        assert result["is_regular_user"] is False
        assert result["user_permissions"] == []

    @pytest.mark.django_db
    def test_staff_user_gets_all_permissions(self):
        """Lines 44-45: is_staff user → user_permissions = ['all']."""
        user = UserFactory(is_staff=True)
        request = HttpRequest()
        request.user = user
        result = user_permissions(request)
        assert result["is_admin_user"] is True
        assert result["user_permissions"] == ["all"]
        assert result["base_url_prefix"] == "/admin"

    @pytest.mark.django_db
    def test_regular_user_with_group_permission_returns_perms(self):
        """Lines 50-61: non-staff user with group permissions → is_regular_user + perms list."""
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(user.__class__)
        group = Group.objects.create(name=f"grp_{user.pk}")
        perm = Permission.objects.create(
            codename=f"test_grp_perm_{user.pk}",
            name="Test Group Permission",
            content_type=ct,
        )
        group.permissions.add(perm)
        user.groups.add(group)

        request = HttpRequest()
        request.user = user
        result = user_permissions(request)

        assert result["is_regular_user"] is True
        assert result["base_url_prefix"] == "/user"
        assert result["url_namespace"] == "custom_admin"
        assert f"test_grp_perm_{user.pk}" in result["user_permissions"]

    @pytest.mark.django_db
    def test_regular_user_with_direct_permission_returns_perms(self):
        """Lines 55-61: non-staff user with direct user_permissions → is_regular_user + perms."""
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(user.__class__)
        perm, _ = Permission.objects.get_or_create(
            codename=f"direct_perm_{user.pk}",
            defaults={
                "name": "Direct User Permission",
                "content_type": ct,
            },
        )
        user.user_permissions.add(perm)

        request = HttpRequest()
        request.user = user
        result = user_permissions(request)

        assert result["is_regular_user"] is True
        assert result["url_namespace"] == "custom_admin"
        assert f"direct_perm_{user.pk}" in result["user_permissions"]
