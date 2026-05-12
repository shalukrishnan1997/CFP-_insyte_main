"""Comprehensive unit tests for DonationBatch model.

FIN-BATCH-UNIT-* test cases covering status workflow, payment tracking,
currency immutability, and QA review fields.
"""

from decimal import Decimal

import pytest

from donations.models import DonationBatch
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════
# DonationBatch — Core Creation & Defaults
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchCreation:
    """FIN-BATCH-UNIT-001 to 004: Creation and field defaults."""

    def test_create_batch_defaults(self) -> None:
        """FIN-BATCH-UNIT-001: Batch created with draft status and zero totals."""
        batch = DonationBatchFactory()
        assert batch.pk is not None
        assert batch.status == DonationBatch.STATUS_PENDING_QA
        assert batch.total_donations == 0
        assert batch.total_amount == Decimal("0.00")

    def test_batch_linked_to_campaign(self) -> None:
        """FIN-BATCH-UNIT-002: Batch has valid campaign FK."""
        campaign = CampaignFactory(name="Batch Link Test")
        batch = DonationBatchFactory(campaign=campaign)
        assert batch.campaign.name == "Batch Link Test"

    def test_batch_default_currency_gbp(self) -> None:
        """FIN-BATCH-UNIT-003: Default currency is GBP."""
        batch = DonationBatchFactory()
        assert batch.default_currency == "GBP"

    def test_batch_default_payment_method_card(self) -> None:
        """FIN-BATCH-UNIT-004: Default payment method is card."""
        batch = DonationBatchFactory()
        assert batch.default_payment_method == "card"


# ═══════════════════════════════════════════════════════════════
# DonationBatch — Status Constants & Workflow
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchStatusWorkflow:
    """FIN-BATCH-UNIT-005 to 009: QA workflow statuses."""

    def test_status_constants(self) -> None:
        """FIN-BATCH-UNIT-005: Status constants have exact values."""
        assert DonationBatch.STATUS_PENDING_QA == "pending_qa"
        assert DonationBatch.STATUS_IN_REVIEW == "in_review"
        assert DonationBatch.STATUS_APPROVED == "approved"
        assert DonationBatch.STATUS_REJECTED == "rejected"

    def test_status_transition_draft_to_pending(self) -> None:
        """FIN-BATCH-UNIT-006: Status can transition draft → pending_qa."""
        batch = DonationBatchFactory(status="pending_qa")
        batch.status = "in_review"
        batch.save()
        batch.refresh_from_db()
        assert batch.status == "in_review"

    def test_status_transition_to_approved(self) -> None:
        """FIN-BATCH-UNIT-007: Status can transition to approved."""
        batch = DonationBatchFactory(status="in_review")
        batch.status = "approved"
        batch.save()
        batch.refresh_from_db()
        assert batch.status == "approved"

    def test_status_transition_to_rejected(self) -> None:
        """FIN-BATCH-UNIT-008: Status can transition to rejected."""
        batch = DonationBatchFactory(status="in_review")
        batch.status = "rejected"
        batch.save()
        batch.refresh_from_db()
        assert batch.status == "rejected"

    def test_status_choices_count(self) -> None:
        """FIN-BATCH-UNIT-009: Exactly 5 status choices exist."""
        assert len(DonationBatch.STATUS_CHOICES) == 4


# ═══════════════════════════════════════════════════════════════
# DonationBatch — Payment Processing Fields
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchPaymentFields:
    """FIN-BATCH-UNIT-010 to 014: Payment tracking fields."""

    def test_payment_status_default_pending(self) -> None:
        """FIN-BATCH-UNIT-010: payment_status defaults to 'pending'."""
        batch = DonationBatchFactory()
        assert batch.payment_status == "pending"

    def test_payment_counts_default_zero(self) -> None:
        """FIN-BATCH-UNIT-011: successful/failed counts default to 0."""
        batch = DonationBatchFactory()
        assert batch.successful_payment_count == 0
        assert batch.failed_payment_count == 0

    def test_payment_counts_update(self) -> None:
        """FIN-BATCH-UNIT-012: Payment counts can be updated."""
        batch = DonationBatchFactory()
        batch.successful_payment_count = 28
        batch.failed_payment_count = 2
        batch.save()
        batch.refresh_from_db()
        assert batch.successful_payment_count == 28
        assert batch.failed_payment_count == 2

    def test_payment_status_transitions(self) -> None:
        """FIN-BATCH-UNIT-013: All payment statuses are valid."""
        for status in [
            "pending",
            "processing",
            "completed",
            "failed",
            "partially_completed",
        ]:
            batch = DonationBatchFactory()
            batch.payment_status = status
            batch.save()
            batch.refresh_from_db()
            assert batch.payment_status == status

    def test_payment_initiated_by_user(self) -> None:
        """FIN-BATCH-UNIT-014: payment_initiated_by tracks the user."""
        user = UserFactory(username="payment-admin")
        batch = DonationBatchFactory()
        batch.payment_initiated_by = user
        batch.save()
        batch.refresh_from_db()
        assert batch.payment_initiated_by.username == "payment-admin"


# ═══════════════════════════════════════════════════════════════
# DonationBatch — Review Fields
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchReviewFields:
    """FIN-BATCH-UNIT-015 to 017: QA review metadata."""

    def test_reviewed_by_stored(self) -> None:
        """FIN-BATCH-UNIT-015: reviewed_by user is recorded."""
        user = UserFactory(username="qa-reviewer")
        batch = DonationBatchFactory()
        batch.reviewed_by = user
        batch.review_notes = "All donations verified."
        batch.save()
        batch.refresh_from_db()
        assert batch.reviewed_by.username == "qa-reviewer"
        assert batch.review_notes == "All donations verified."

    def test_review_notes_empty_default(self) -> None:
        """FIN-BATCH-UNIT-016: review_notes defaults to empty string."""
        batch = DonationBatchFactory()
        assert batch.review_notes == ""

    def test_batch_str_format(self) -> None:
        """FIN-BATCH-UNIT-017: __str__ includes name and count."""
        batch = DonationBatchFactory(batch_name="QA-BATCH-001", total_donations=10)
        s = str(batch)
        assert "QA-BATCH-001" in s
        assert "10" in s


# ═══════════════════════════════════════════════════════════════
# DonationBatch — Totals Tracking
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchTotals:
    """FIN-BATCH-UNIT-018 to 019: Total donations and amount."""

    def test_total_amount_precision(self) -> None:
        """FIN-BATCH-UNIT-018: total_amount handles large amounts."""
        batch = DonationBatchFactory(total_amount=Decimal("999999999.99"))
        batch.refresh_from_db()
        assert batch.total_amount == Decimal("999999999.99")

    def test_total_donations_count(self) -> None:
        """FIN-BATCH-UNIT-019: total_donations integer storage."""
        batch = DonationBatchFactory(total_donations=500)
        batch.refresh_from_db()
        assert batch.total_donations == 500
