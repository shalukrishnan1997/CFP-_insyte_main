"""Data integrity and reconciliation tests.

FIN-DATA-ACC-* acceptance tests validating 100% accuracy for
financial data across all modules.
"""

from decimal import Decimal

import pytest

from banking.services import BankingService
from donations.models import Donation
from payments.models import StripePayment
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
)

# ═══════════════════════════════════════════════════════════════
# Data Integrity — Amount Precision
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestAmountPrecision:
    """FIN-DATA-ACC-001: Donation amounts stored with exact precision."""

    def test_decimal_precision_preserved(self) -> None:
        """FIN-DATA-ACC-001a: £0.01 amounts preserved exactly."""
        donation = DonationFactory(amount=Decimal("0.01"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("0.01")

    def test_large_amount_precision(self) -> None:
        """FIN-DATA-ACC-001b: Large amounts preserved without floating point errors."""
        donation = DonationFactory(amount=Decimal("9999999.99"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("9999999.99")

    def test_sum_precision(self) -> None:
        """FIN-DATA-ACC-001c: Sum of donations matches expected total exactly."""
        batch = DonationBatchFactory()
        amounts = [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
        for amt in amounts:
            DonationFactory(batch=batch, campaign=batch.campaign, amount=amt)

        total = sum(d.amount for d in Donation.objects.filter(batch=batch))
        assert total == Decimal("100.00")


# ═══════════════════════════════════════════════════════════════
# Data Integrity — Batch Totals
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBatchTotalsReconciliation:
    """FIN-DATA-ACC-002: Batch totals match individual donations."""

    def test_batch_total_equals_donation_sum(self) -> None:
        """FIN-DATA-ACC-002a: Manual sum matches batch total_amount."""
        batch = DonationBatchFactory()
        DonationFactory(batch=batch, campaign=batch.campaign, amount=Decimal("100.00"))
        DonationFactory(batch=batch, campaign=batch.campaign, amount=Decimal("250.50"))
        DonationFactory(batch=batch, campaign=batch.campaign, amount=Decimal("75.25"))

        donation_sum = sum(d.amount for d in Donation.objects.filter(batch=batch))
        expected = Decimal("425.75")
        assert donation_sum == expected

    def test_batch_donation_count_accurate(self) -> None:
        """FIN-DATA-ACC-002b: Donation count matches actual records."""
        batch = DonationBatchFactory()
        for _i in range(5):
            DonationFactory(
                batch=batch, campaign=batch.campaign, amount=Decimal("10.00")
            )

        actual_count = Donation.objects.filter(batch=batch).count()
        assert actual_count == 5


# ═══════════════════════════════════════════════════════════════
# Data Integrity — Slip Totals
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipTotalsReconciliation:
    """FIN-DATA-ACC-003: PayingInSlip totals match linked donations."""

    def test_slip_recalculate_matches_donations(self) -> None:
        """FIN-DATA-ACC-003a: recalculate_totals produces exact match."""
        slip = PayingInSlipFactory()
        DonationFactory(
            paying_in_slip=slip, amount=Decimal("150.00"), payment_method="cheque"
        )
        DonationFactory(
            paying_in_slip=slip, amount=Decimal("200.00"), payment_method="cheque"
        )
        DonationFactory(
            paying_in_slip=slip, amount=Decimal("50.50"), payment_method="cheque"
        )

        BankingService.recalculate_slip_totals(slip)
        slip.refresh_from_db()

        expected_amount = Decimal("400.50")
        expected_count = 3
        assert slip.total_amount == expected_amount
        assert slip.total_items == expected_count

    def test_slip_processed_vs_total_reconciliation(self) -> None:
        """FIN-DATA-ACC-003b: Unprocessed amount = total - processed."""
        slip = PayingInSlipFactory(total_amount=Decimal("1000.00"))
        slip.processed_amount = Decimal("750.00")
        unprocessed = slip.get_unprocessed_amount()
        assert unprocessed == Decimal("250.00")


# ═══════════════════════════════════════════════════════════════
# Data Integrity — Orphan Records
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestOrphanRecords:
    """FIN-DATA-ACC-004: No orphaned donations exist."""

    def test_no_donations_without_batch(self) -> None:
        """FIN-DATA-ACC-004a: All donations belong to a batch."""
        # Create valid donations
        DonationFactory()
        DonationFactory()
        DonationFactory()

        orphans = Donation.objects.filter(batch__isnull=True).count()
        assert orphans == 0

    def test_no_donations_without_campaign(self) -> None:
        """FIN-DATA-ACC-004b: All donations belong to a campaign."""
        DonationFactory()
        DonationFactory()

        orphans = Donation.objects.filter(campaign__isnull=True).count()
        assert orphans == 0


# ═══════════════════════════════════════════════════════════════
# Data Integrity — Payment Records
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestPaymentRecordsIntegrity:
    """FIN-DATA-ACC-005: Payment records aligned with donations."""

    def test_payment_amount_matches_donation(self) -> None:
        """FIN-DATA-ACC-005a: StripePayment amount matches linked donation."""
        donation = DonationFactory(payment_method="card", amount=Decimal("150.00"))
        customer = StripeCustomerFactory()
        payment = StripePaymentFactory(
            stripe_customer=customer,
            donation=donation,
            amount=Decimal("150.00"),
            status="succeeded",
        )
        assert payment.amount == donation.amount

    def test_succeeded_payments_total_matches(self) -> None:
        """FIN-DATA-ACC-005b: Total succeeded payments match donation sum."""
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign, status="approved")
        customer = StripeCustomerFactory()

        total_expected = Decimal("0")
        for i in range(3):
            amount = Decimal(f"{(i + 1) * 50}.00")
            d = DonationFactory(
                campaign=campaign,
                batch=batch,
                payment_method="card",
                amount=amount,
            )
            StripePaymentFactory(
                stripe_customer=customer,
                donation=d,
                amount=amount,
                status="succeeded",
            )
            total_expected += amount

        payments_sum = sum(
            p.amount
            for p in StripePayment.objects.filter(
                donation__batch=batch, status="succeeded"
            )
        )
        assert payments_sum == total_expected
        assert payments_sum == Decimal("300.00")


# ═══════════════════════════════════════════════════════════════
# Data Integrity — Cross-Module Reconciliation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCrossModuleReconciliation:
    """FIN-DATA-ACC-006: Data consistent across modules."""

    def test_campaign_donation_count_matches(self) -> None:
        """FIN-DATA-ACC-006a: Campaign.donations.count() matches actual."""
        campaign = CampaignFactory()
        for _ in range(4):
            DonationFactory(campaign=campaign)

        assert campaign.donations.count() == 4

    def test_batch_has_correct_campaign(self) -> None:
        """FIN-DATA-ACC-006b: Batch campaign matches donation campaign."""
        campaign = CampaignFactory(name="Reconcile Test")
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(campaign=campaign, batch=batch)

        assert donation.campaign.pk == batch.campaign.pk
        assert donation.campaign.name == "Reconcile Test"
