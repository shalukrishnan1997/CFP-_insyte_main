"""Unit tests for qa_approve_batch - Daily Banking visibility fix.

Regression tests ensuring that when a QA batch is approved via the
``qa_approve_batch`` view, all pending / flagged donations are promoted
to ``qa_status = "approved"`` so they appear in the Daily Banking view
(which filters on ``donation.qa_status``).

Related fix: custom_admin/views/qa_review.py - qa_approve_batch()
"""

import pytest
from django.http import HttpRequest
from django.test import RequestFactory
from django.utils import timezone

from custom_admin.views import qa_review
from donations.models import Donation, DonationBatch
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_message(_request: object, _message: object) -> None:
    """Suppress Django flash messages during unit tests."""


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    # Mock log_request_action to avoid audit log database constraints in tests
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_args, **_kwargs: None)
    # Suppress the dashboard-stats version bump so the cache stays untouched
    # — the production call replaces the older ``cache.delete`` invalidation.
    monkeypatch.setattr(qa_review, "_bump_dashboard_stats_version", lambda: None)


def _staff_post_request(url: str = "/admin/qa/batch/approve/") -> HttpRequest:
    """Return a POST RequestFactory instance (without user attached)."""
    return RequestFactory().post(url)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestQaApproveBatchDonationStatus:
    """Verify donation-level qa_status is bulk-upgraded when a batch is approved."""

    # ------------------------------------------------------------------
    # Core fix: pending donations become approved
    # ------------------------------------------------------------------

    def test_pending_donations_become_approved_on_batch_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All pending donations must be set to approved when the batch is approved."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )
        donations = DonationFactory.create_batch(
            9,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        request = _staff_post_request()
        request.user = staff

        # Manually pre-approve each donation (simulating the per-donation review)
        for d in donations:
            d.qa_status = Donation.QA_STATUS_APPROVED
            d.save(update_fields=["qa_status", "updated_at"])

        # Verify all are approved before calling the view (sanity)
        assert (
            batch.donations.filter(qa_status=Donation.QA_STATUS_APPROVED).count() == 9
        )

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        # All donations must remain approved
        for d in donations:
            d.refresh_from_db()
            assert d.qa_status == Donation.QA_STATUS_APPROVED, (
                f"Donation {d.id} was not approved"
            )

    def test_batch_approve_upgrades_residual_pending_donations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        If any donations were missed during per-donation review (still pending),
        qa_approve_batch should promote them to approved.

        This is the primary regression case.
        """
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )

        # 7 approved + 2 residual pending (edge case)
        approved_donations = DonationFactory.create_batch(
            7,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        pending_donations = DonationFactory.create_batch(
            2,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        # View approves the batch and auto-promotes all residual pending donations.
        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        for d in approved_donations + pending_donations:
            d.refresh_from_db()
            assert d.qa_status == Donation.QA_STATUS_APPROVED

    def test_batch_approve_blocks_unresolved_card_donations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual card donations that still need secure payment details block approval."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="card",
        )
        blocked_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        assert str(blocked_donation.id) in response.url
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

        blocked_donation.refresh_from_db()
        assert blocked_donation.qa_status == Donation.QA_STATUS_PENDING

    def test_batch_approve_blocks_pending_qa_redaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        redaction_required_all: object,
    ) -> None:
        """Residual scanned donations must be redacted in QA before batch approval."""
        del redaction_required_all
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="cheque",
        )
        blocked_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )
        ScanPlaceholderFactory(
            donation=blocked_donation,
            redaction_status="pending",
            image_url="",
            image_path="ScanOutput/demo/batch_redaction_pending.png",
            page_keys=["ScanOutput/demo/batch_redaction_pending.png"],
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        assert str(blocked_donation.id) in response.url
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_batch_approve_allowed_when_redaction_deferred(
        self,
        monkeypatch: pytest.MonkeyPatch,
        redaction_required_all: object,
    ) -> None:
        """Coords-saved (DEFERRED) status counts as redaction-ready for approval.

        Cheque has no charge step, so on batch approval the redaction task
        fires immediately for the deferred placeholder.
        """
        del redaction_required_all
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="cheque",
        )
        deferred_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )
        deferred_placeholder = ScanPlaceholderFactory(
            donation=deferred_donation,
            redaction_status="deferred",
            image_url="",
            image_path="ScanOutput/demo/deferred.png",
            page_keys=["ScanOutput/demo/deferred.png"],
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]
            ],
        )

        from scans import tasks

        delay_calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            tasks.apply_deferred_redaction_task,
            "delay",
            lambda *args: delay_calls.append(args),
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        deferred_donation.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert deferred_donation.qa_status == Donation.QA_STATUS_APPROVED
        # Non-card method → redaction enqueued at batch approval.
        assert delay_calls == [(str(deferred_placeholder.id),)]

    def test_batch_approve_does_not_redact_card_until_charge(
        self,
        monkeypatch: pytest.MonkeyPatch,
        redaction_required_all: object,
    ) -> None:
        """Card donations defer redaction until their Stripe charge succeeds.

        Approving the batch must NOT enqueue the redaction task for card
        donations — that happens from ``BatchPaymentService`` after the
        successful PaymentIntent.
        """
        del redaction_required_all
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="card",
        )
        # Card donation pre-charged so the per-donation card-blocking gate
        # in qa_approve_batch lets the batch through.
        card_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="completed",
            qa_status=Donation.QA_STATUS_PENDING,
        )
        ScanPlaceholderFactory(
            donation=card_donation,
            redaction_status="deferred",
            image_url="",
            image_path="ScanOutput/demo/card_deferred.png",
            page_keys=["ScanOutput/demo/card_deferred.png"],
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]
            ],
        )

        from scans import tasks

        delay_calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            tasks.apply_deferred_redaction_task,
            "delay",
            lambda *args: delay_calls.append(args),
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        # Card donation: redaction must NOT fire on batch approve. It will
        # fire from the post-charge hook instead.
        assert delay_calls == []

    def test_batch_approve_allows_prepaid_card_donations_to_auto_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual card donations with completed payments may still auto-approve."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="card",
        )
        prepaid_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="completed",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        prepaid_donation.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert prepaid_donation.qa_status == Donation.QA_STATUS_APPROVED

    # ------------------------------------------------------------------
    # Rejected donations must NOT be overwritten
    # ------------------------------------------------------------------

    def test_rejected_donations_preserved_on_batch_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Intentionally rejected donations must stay rejected after batch approval."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )

        # 8 approved + 1 rejected (deliberate)
        DonationFactory.create_batch(
            8,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        rejected_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_REJECTED,
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        rejected_donation.refresh_from_db()
        assert rejected_donation.qa_status == Donation.QA_STATUS_REJECTED, (
            "Rejected donation must not be overwritten to approved"
        )

    # ------------------------------------------------------------------
    # Flagged donations should be promoted to approved
    # ------------------------------------------------------------------

    def test_flagged_donations_promoted_to_approved_on_batch_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flagged donations (not rejected) should be promoted to approved."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="cheque",
        )

        approved = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        flagged = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_FLAGGED,
        )

        # The pending guard looks at pending donations only; flagged counts
        # as non-pending for the batch totals. Force override by setting flagged
        # to approved before calling (simulating the guard bypass path)
        flagged.qa_status = Donation.QA_STATUS_APPROVED
        flagged.save(update_fields=["qa_status", "updated_at"])

        request = _staff_post_request()
        request.user = staff
        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        approved.refresh_from_db()
        flagged.refresh_from_db()
        assert approved.qa_status == Donation.QA_STATUS_APPROVED
        assert flagged.qa_status == Donation.QA_STATUS_APPROVED

    # ------------------------------------------------------------------
    # Daily Banking filter compatibility
    # ------------------------------------------------------------------

    def test_approved_caf_donations_visible_in_banking_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        After qa_approve_batch, CAF donations must match base_unassigned_filter.

        This is the exact scenario from the bug report:
        OCR-ABC1 ABC1PDF26 CAF batch approved → 9 CAF donations must appear
        in Daily Banking.
        """
        from banking.utils import base_unassigned_filter

        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )

        today = timezone.now().date()
        donations = DonationFactory.create_batch(
            9,
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_APPROVED,  # pre-approved per-donation
            donation_date=today,
            paying_in_slip=None,
        )

        request = _staff_post_request()
        request.user = staff
        response = qa_review.qa_approve_batch(request, batch.id)
        assert response.status_code == 302

        # Apply the exact same filter Daily Banking uses
        base_q = base_unassigned_filter(today, today)
        visible_ids = set(Donation.objects.filter(base_q).values_list("id", flat=True))
        donation_ids = {d.id for d in donations}

        assert donation_ids.issubset(visible_ids), (
            "Not all approved CAF donations are visible in the Daily Banking filter"
        )

    # ------------------------------------------------------------------
    # Batch status field
    # ------------------------------------------------------------------

    def test_batch_status_is_approved_after_batch_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Batch.status must be STATUS_APPROVED after successful approval."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(status=DonationBatch.STATUS_PENDING_QA)
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        request = _staff_post_request()
        request.user = staff
        qa_review.qa_approve_batch(request, batch.id)

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

    def test_batch_reviewed_by_and_at_set_on_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reviewed_by and reviewed_at must be populated when batch is approved."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(status=DonationBatch.STATUS_PENDING_QA)
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        request = _staff_post_request()
        request.user = staff
        qa_review.qa_approve_batch(request, batch.id)

        batch.refresh_from_db()
        assert batch.reviewed_by == staff
        assert batch.reviewed_at is not None
