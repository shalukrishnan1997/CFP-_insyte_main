"""Tests for QA access control.

The decisive contract: QA-group users keep working when ``DonationBatch``
migrates between Django apps. The original ``core`` → ``donations`` split
left existing QA-group permissions keyed on ``core.view_donationbatch``;
the access helper must accept either app label via suffix matching, not
hardcode the new label.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from custom_admin.views.qa_utils import has_qa_access
from donations.models import DonationBatch
from tests.factories import UserFactory


@pytest.mark.django_db()
class TestHasQaAccess:
    """``has_qa_access`` must accept stale ``core.view_donationbatch`` perms."""

    def test_superuser_has_access(self) -> None:
        user = UserFactory(is_superuser=True, is_staff=False)
        assert has_qa_access(user) is True

    def test_staff_has_access(self) -> None:
        user = UserFactory(is_staff=True, is_superuser=False)
        assert has_qa_access(user) is True

    def test_admin_group_has_access(self) -> None:
        user = UserFactory(is_staff=False, is_superuser=False)
        admin_group, _ = Group.objects.get_or_create(name="admin")
        user.groups.add(admin_group)
        assert has_qa_access(user) is True

    def test_qa_group_has_access(self) -> None:
        """A user in the QA group is granted access regardless of permissions."""
        user = UserFactory(is_staff=False, is_superuser=False)
        qa_group, _ = Group.objects.get_or_create(name="QA")
        user.groups.add(qa_group)
        assert has_qa_access(user) is True

    def test_donations_view_donationbatch_perm_grants_access(self) -> None:
        """The post-refactor permission name still works."""
        user = UserFactory(is_staff=False, is_superuser=False)
        ct = ContentType.objects.get_for_model(DonationBatch)
        perm = Permission.objects.get(codename="view_donationbatch", content_type=ct)
        user.user_permissions.add(perm)
        assert has_qa_access(user) is True

    def test_legacy_core_view_donationbatch_perm_grants_access(self) -> None:
        """A user who still holds the pre-split ``core.view_donationbatch`` permission
        must keep QA access — that is the bug the suffix-match fix closes.
        """
        user = UserFactory(is_staff=False, is_superuser=False)
        # Synthesize the legacy permission row that would exist for users
        # provisioned before the app-split refactor. Use a non-donations
        # content type so the resulting permission string is
        # ``<app>.view_donationbatch`` with ``<app>`` != ``donations``.
        legacy_ct, _ = ContentType.objects.get_or_create(
            app_label="core", model="donationbatch"
        )
        legacy_perm, _ = Permission.objects.get_or_create(
            codename="view_donationbatch",
            content_type=legacy_ct,
            defaults={"name": "Legacy view donation batch"},
        )
        user.user_permissions.add(legacy_perm)
        assert has_qa_access(user) is True

    def test_user_without_perms_is_denied(self) -> None:
        user = UserFactory(is_staff=False, is_superuser=False)
        assert has_qa_access(user) is False
