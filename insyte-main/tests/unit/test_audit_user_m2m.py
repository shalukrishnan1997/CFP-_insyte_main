"""Tests for the m2m_changed audit signal on User.groups / user_permissions."""

import pytest
from django.contrib.auth.models import Group, Permission

from audit.models import AuditLog
from tests.factories import UserFactory


@pytest.mark.django_db()
class TestUserGroupsAudit:
    """Membership changes on User.groups leave an AuditLog entry."""

    def test_adding_group_creates_audit_entry(self) -> None:
        user = UserFactory()
        group = Group.objects.create(name="QA Reviewers")

        before = AuditLog.objects.filter(
            model_name="User", object_id=str(user.pk)
        ).count()

        user.groups.add(group)

        entries = AuditLog.objects.filter(model_name="User", object_id=str(user.pk))
        assert entries.count() > before
        added_entries = [e for e in entries if e.changes.get("action") == "post_add"]
        assert added_entries
        assert any("groups" in e.changes.get("relation", "") for e in added_entries)

    def test_removing_group_creates_audit_entry(self) -> None:
        user = UserFactory()
        group = Group.objects.create(name="Scan Operators")
        user.groups.add(group)

        user.groups.remove(group)

        removed_entries = AuditLog.objects.filter(
            model_name="User",
            object_id=str(user.pk),
        ).filter(changes__action="post_remove")
        assert removed_entries.exists()


@pytest.mark.django_db()
class TestUserPermissionsAudit:
    """Direct permission changes on User.user_permissions are audited."""

    def test_adding_permission_creates_audit_entry(self) -> None:
        user = UserFactory()
        perm = Permission.objects.first()
        assert perm is not None, "Expected at least one permission in test DB"

        user.user_permissions.add(perm)

        entries = AuditLog.objects.filter(
            model_name="User",
            object_id=str(user.pk),
        ).filter(changes__relation="user_permissions")
        assert entries.exists()
