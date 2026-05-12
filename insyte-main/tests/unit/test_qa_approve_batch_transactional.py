"""Tests for transactional batch approval and double-click idempotency.

Covers three regression scenarios introduced by Unit #7 of the scan-workflow
audit:

* The Approve action is **idempotent** — submitting it twice for the same
  batch must not re-fire the ``DonationBatch`` ``post_save`` signal and
  therefore must not duplicate downstream Celery dispatches (status email,
  gift-aid CSV regeneration, letter generation).
* The dropdown-driven ``qa_update_batch_status`` path is also idempotent —
  selecting the batch's current status must be a no-op (no save, no signal,
  no Celery dispatch, no audit log entry). The guard is enforced inside
  ``_commit_batch_status`` so every approval surface inherits it.
* Downstream Celery dispatches in
  :func:`core.signals.notify_batch_status_change` are scheduled via
  :func:`django.db.transaction.on_commit` so they only fire after a
  successful database commit. A rollback inside ``_commit_batch_status``'s
  atomic block must therefore drop the dispatches *and* skip the audit log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from audit.models import AuditLog
from custom_admin.views import qa_review
from donations.models import Donation, DonationBatch
from tests.factories import DonationBatchFactory, DonationFactory, UserFactory

if TYPE_CHECKING:
    from core.models import User


def _noop_message(_request: object, _message: object) -> None:
    """Suppress Django flash messages during unit tests."""


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence flash messages, audit logging, and cache writes."""
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_a, **_kw: None)
    monkeypatch.setattr(qa_review, "_bump_dashboard_stats_version", lambda: None)


def _patch_messages_keep_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence flash messages and cache writes but leave audit logging alone."""
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    monkeypatch.setattr(qa_review, "_bump_dashboard_stats_version", lambda: None)


def _staff_post_request() -> HttpRequest:
    """Return a staff-style POST request without a user attached."""
    return RequestFactory().post("/admin/qa/batch/approve/")


def _patch_celery_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock]:
    """Replace the two Celery dispatches with mocks; return ``(email, approved)``."""
    from core import tasks as core_tasks

    email_mock = MagicMock(name="send_batch_status_email_delay")
    approved_mock = MagicMock(name="on_batch_approved_task_delay")
    monkeypatch.setattr(
        core_tasks.send_batch_status_email, "delay", email_mock, raising=True
    )
    monkeypatch.setattr(
        core_tasks.on_batch_approved_task, "delay", approved_mock, raising=True
    )
    return email_mock, approved_mock


@pytest.mark.django_db(transaction=True)
class TestQaApproveBatchIdempotent:
    """A double-click on Approve must not duplicate downstream side-effects."""

    def test_double_submit_dispatches_celery_tasks_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second approve POST is a no-op once the batch is already approved."""
        _patch_messages(monkeypatch)
        email_mock, approved_mock = _patch_celery_dispatches(monkeypatch)

        staff: User = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
            created_by=staff,
        )
        DonationFactory.create_batch(
            3,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        request = _staff_post_request()
        request.user = staff

        first = qa_review.qa_approve_batch(request, batch.id)
        assert first.status_code == 302

        second = qa_review.qa_approve_batch(request, batch.id)
        assert second.status_code == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert email_mock.call_count == 1, (
            f"Expected exactly one status email dispatch, got {email_mock.call_count}"
        )
        assert approved_mock.call_count == 1, (
            f"Expected exactly one on_batch_approved_task dispatch, "
            f"got {approved_mock.call_count}"
        )


@pytest.mark.django_db(transaction=True)
class TestBatchApprovalDispatchesGatedOnCommit:
    """Downstream Celery dispatches must fire only after a successful commit."""

    def test_rollback_skips_celery_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure inside ``_commit_batch_status`` rolls back every side effect.

        Drives the production atomic block by calling ``_commit_batch_status``
        directly with ``log_request_action`` monkeypatched to raise. Verifies
        that the batch save, donation cascade, audit log, and Celery dispatches
        all roll back together.
        """
        _patch_messages_keep_audit(monkeypatch)
        email_mock, approved_mock = _patch_celery_dispatches(monkeypatch)

        staff: User = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
            created_by=staff,
        )
        DonationFactory.create_batch(
            2,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced audit failure")

        monkeypatch.setattr(qa_review, "log_request_action", _boom)

        request = _staff_post_request()
        request.user = staff

        # Mutate batch in-memory the way qa_approve_batch does, then drive the
        # central commit helper inside the production atomic boundary.
        batch.status = DonationBatch.STATUS_APPROVED  # type: ignore[assignment]
        batch.reviewed_by = staff  # type: ignore[assignment]

        audit_count_before = AuditLog.objects.count()

        with pytest.raises(RuntimeError, match="forced audit failure"):
            qa_review._commit_batch_status(
                request,
                batch,
                summary="QA approved batch (rollback test)",
                changes={
                    "old_status": DonationBatch.STATUS_PENDING_QA,
                    "new_status": DonationBatch.STATUS_APPROVED,
                },
            )

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA, (
            "Atomic block must roll back the status flip"
        )
        # Cascade donation update must also roll back.
        pending_after = batch.donations.filter(
            qa_status=Donation.QA_STATUS_PENDING
        ).count()
        assert pending_after == 2, (
            "Donation cascade update must roll back with the batch save"
        )
        assert AuditLog.objects.count() == audit_count_before, (
            "Audit log entry must NOT persist on rollback"
        )
        assert email_mock.call_count == 0, (
            "Status email must NOT dispatch on rollback "
            f"(got {email_mock.call_count} call(s))"
        )
        assert approved_mock.call_count == 0, (
            "on_batch_approved_task must NOT dispatch on rollback "
            f"(got {approved_mock.call_count} call(s))"
        )

    def test_successful_commit_dispatches_both_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean approve must dispatch both Celery tasks once after commit."""
        _patch_messages(monkeypatch)
        email_mock, approved_mock = _patch_celery_dispatches(monkeypatch)

        staff: User = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
            created_by=staff,
        )
        DonationFactory.create_batch(
            2,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        request = _staff_post_request()
        request.user = staff
        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert email_mock.call_count == 1
        assert approved_mock.call_count == 1


@pytest.mark.django_db(transaction=True)
class TestQaUpdateBatchStatusIdempotent:
    """The dropdown path inherits the central idempotency guard."""

    def test_dropdown_no_change_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Selecting the batch's current status must not trigger any side effect.

        Verifies the guard inside ``_commit_batch_status``: no save, no signal
        fire (so no Celery dispatch), and no audit log entry are produced
        when ``new_status == old_status``.
        """
        _patch_messages_keep_audit(monkeypatch)
        email_mock, approved_mock = _patch_celery_dispatches(monkeypatch)

        staff: User = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_APPROVED,
            default_payment_method="caf",
            created_by=staff,
        )
        DonationFactory.create_batch(
            2,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        audit_count_before = AuditLog.objects.count()
        original_reviewed_at = batch.reviewed_at

        request = RequestFactory().post(
            f"/admin/qa/batch/{batch.id}/update-status/",
            data={"status": DonationBatch.STATUS_APPROVED, "review_notes": ""},
        )
        request.user = staff

        response = qa_review.qa_update_batch_status(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        # Status unchanged.
        assert batch.status == DonationBatch.STATUS_APPROVED
        # ``reviewed_at`` must not have been overwritten — the no-op guard
        # short-circuits before save.
        assert batch.reviewed_at == original_reviewed_at, (
            "reviewed_at must not be touched on a no-op status update"
        )
        # No Celery dispatches because no post_save fired.
        assert email_mock.call_count == 0, (
            "Status email must NOT dispatch on no-op status update "
            f"(got {email_mock.call_count} call(s))"
        )
        assert approved_mock.call_count == 0, (
            "on_batch_approved_task must NOT dispatch on no-op status update "
            f"(got {approved_mock.call_count} call(s))"
        )
        # No audit log written.
        assert AuditLog.objects.count() == audit_count_before, (
            "Audit log entry must NOT be written on a no-op status update"
        )
