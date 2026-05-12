"""Regression tests for audit log helpers."""

import pytest

from audit.models import AuditLog
from audit.utils import log_action
from core.utils import log_audit
from tests.factories import UserFactory


@pytest.mark.django_db()
class TestAuditLogHelpers:
    """Verify audit helpers handle optional text fields safely."""

    def test_log_action_uses_empty_strings_for_missing_text_fields(self) -> None:
        """log_action should avoid NULL values for NOT NULL audit text fields."""
        log_action(
            user=None,
            action="READ",
            model_name="Campaign",
            object_id=None,
            batch_id=None,
        )

        audit_log = AuditLog.objects.get()
        assert audit_log.object_id == ""
        assert audit_log.batch_id == ""

    def test_log_audit_uses_empty_strings_for_missing_text_fields(self) -> None:
        """log_audit should avoid NULL values for NOT NULL audit text fields."""
        user = UserFactory()

        log_audit(
            user=user,
            action="READ",
            model_name="Campaign",
            object_id=None,
            batch_id=None,
        )

        audit_log = AuditLog.objects.get(user=user)
        assert audit_log.object_id == ""
        assert audit_log.batch_id == ""
