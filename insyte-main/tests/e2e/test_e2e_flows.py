"""End-to-end flow integration tests.

FIN-E2E-SYS-* test cases covering the complete application flows
from Campaign creation through to final reporting/verification.
These tests validate data flows across module boundaries.
"""

from decimal import Decimal

import pytest

from banking.services import BankingService
from donations.models import Donation
from payments.models import StripePayment
from payments.services import StripePaymentService
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    PayingInSlipFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════
# E2E Flow 1: Card Payment Flow
# Campaign → Donation → QA → Payment Processing → Verify
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCardPaymentE2E:
    """FIN-E2E-SYS-001: Complete card payment flow."""

    def test_full_card_donation_lifecycle(self) -> None:
        """FIN-E2E-SYS-001: Campaign → Batch → Donations → QA → Payment records."""
        # Step 1: Create campaign & client
        client = ClientFactory(name="E2E Card Client")
        campaign = CampaignFactory(
            client=client,
            name="E2E Card Campaign",
            status="active",
        )

        # Step 2: Create batch with donations
        batch = DonationBatchFactory(
            campaign=campaign,
            batch_name="E2E-CARD-001",
            status="draft",
        )
        donor1 = DonorFactory(urn="E2E001")
        donor2 = DonorFactory(urn="E2E002")

        d1 = DonationFactory(
            campaign=campaign,
            batch=batch,
            donor=donor1,
            payment_method="card",
            amount=Decimal("50.00"),
        )
        d2 = DonationFactory(
            campaign=campaign,
            batch=batch,
            donor=donor2,
            payment_method="card",
            amount=Decimal("75.00"),
        )

        # Step 3: Submit for QA
        batch.status = "pending_qa"
        batch.total_donations = 2
        batch.total_amount = Decimal("125.00")
        batch.save()

        # Step 4: QA Review — Approve
        batch.status = "approved"
        batch.reviewed_by = UserFactory(username="qa-e2e")
        batch.review_notes = "All entries verified."
        batch.save()
        batch.refresh_from_db()
        assert batch.status == "approved"

        # Step 5: Mark donations as approved
        d1.qa_status = "approved"
        d1.save()
        d2.qa_status = "approved"
        d2.save()

        # Step 6: Simulate payment processing
        customer = StripeCustomerFactory(client=client)
        StripePaymentFactory(
            stripe_payment_intent_id="pi_e2e_001",
            stripe_customer=customer,
            donation=d1,
            amount=Decimal("50.00"),
            status="succeeded",
        )
        StripePaymentFactory(
            stripe_payment_intent_id="pi_e2e_002",
            stripe_customer=customer,
            donation=d2,
            amount=Decimal("75.00"),
            status="succeeded",
        )

        # Step 7: Verify data integrity
        assert (
            StripePayment.objects.filter(donation=d1, status="succeeded").count() == 1
        )
        assert (
            StripePayment.objects.filter(donation=d2, status="succeeded").count() == 1
        )

        # Verify amounts match
        payments = StripePayment.objects.filter(
            donation__batch=batch, status="succeeded"
        )
        total_paid = sum(p.amount for p in payments)
        assert total_paid == Decimal("125.00")


# ═══════════════════════════════════════════════════════════════
# E2E Flow 2: Non-Card Payment Flow (Banking)
# Campaign → Donation → QA → Banking Slip → Verify
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestNonCardPaymentE2E:
    """FIN-E2E-SYS-002: Complete non-card banking flow."""

    def test_full_cheque_banking_lifecycle(self) -> None:
        """FIN-E2E-SYS-002: Campaign → Batch → Cheque Donations → QA → Banking."""
        import datetime

        # Step 1: Setup
        client = ClientFactory(name="E2E Banking Client")
        campaign = CampaignFactory(
            client=client, name="E2E Banking Campaign", status="active"
        )
        batch = DonationBatchFactory(campaign=campaign, status="draft")

        # Step 2: Create cheque donations
        d1 = DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cheque",
            amount=Decimal("200.00"),
            cheque_number="CHQ-E2E-001",
        )
        d2 = DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cash",
            amount=Decimal("50.00"),
        )

        # Step 3: QA Approve
        batch.status = "approved"
        batch.total_donations = 2
        batch.total_amount = Decimal("250.00")
        batch.save()

        d1.qa_status = "approved"
        d1.save()
        d2.qa_status = "approved"
        d2.save()

        # Step 4: Create paying-in slip
        slip_number = BankingService.generate_slip_number(
            client, datetime.date(2026, 2, 25)
        )
        slip = PayingInSlipFactory(
            slip_number=slip_number,
            client=client,
            banking_date=datetime.date(2026, 2, 25),
            payment_type="mixed",
        )

        # Step 5: Link donations to slip
        d1.paying_in_slip = slip
        d1.save()
        d2.paying_in_slip = slip
        d2.save()

        # Step 6: Recalculate totals
        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()
        assert slip.total_amount == Decimal("250.00")
        assert slip.total_items == 2

        # Step 7: Mark as banked
        user = UserFactory(username="banker-e2e")
        BankingService.mark_slip_as_processed(
            slip,
            processed_amount=Decimal("250.00"),
            completion_status="full_success",
            bank_processed_date=datetime.date(2026, 2, 26),
            processed_by=user,
        )
        slip.refresh_from_db()
        assert slip.status == "processed"
        assert slip.processed_amount == Decimal("250.00")


# ═══════════════════════════════════════════════════════════════
# E2E Flow 3: Mixed Batch (Card + Non-Card)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestMixedBatchE2E:
    """FIN-E2E-SYS-003: Mixed payment methods in one batch."""

    def test_mixed_batch_routes_correctly(self) -> None:
        """FIN-E2E-SYS-003: Card goes to payment, non-card to banking."""
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign, status="approved")

        # Card donations
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            amount=Decimal("100.00"),
        )

        # Non-card donations
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cash",
            amount=Decimal("50.00"),
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cheque",
            amount=Decimal("75.00"),
        )

        # Verify filtering
        card_donations = Donation.objects.filter(batch=batch, payment_method="card")
        non_card_donations = Donation.objects.filter(batch=batch).exclude(
            payment_method="card"
        )

        assert card_donations.count() == 1
        assert non_card_donations.count() == 2

        # Card total
        card_total = sum(d.amount for d in card_donations)
        assert card_total == Decimal("100.00")

        # Non-card total
        non_card_total = sum(d.amount for d in non_card_donations)
        assert non_card_total == Decimal("125.00")


# ═══════════════════════════════════════════════════════════════
# E2E Flow 4: Failed Payment & Retry
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestFailedPaymentRetryE2E:
    """FIN-E2E-SYS-004: Payment failure and retry flow."""

    def test_payment_failure_then_retry(self) -> None:
        """FIN-E2E-SYS-004: Failed payment → retry → success."""
        donation = DonationFactory(payment_method="card", amount=Decimal("100.00"))
        customer = StripeCustomerFactory()

        # Step 1: Initial payment fails
        payment = StripePaymentFactory(
            stripe_payment_intent_id="pi_retry_001",
            stripe_customer=customer,
            donation=donation,
            amount=Decimal("100.00"),
            status="failed",
            error_message="Insufficient funds",
            error_code="insufficient_funds",
            retry_count=0,
            max_retries=3,
        )
        assert StripePaymentService.can_retry(payment) is True

        # Step 2: Simulate retry succeeding
        payment.status = "succeeded"
        payment.retry_count = 1
        payment.error_message = ""
        payment.save()
        payment.refresh_from_db()

        assert payment.status == "succeeded"
        assert payment.retry_count == 1
        assert StripePaymentService.can_retry(payment) is False
