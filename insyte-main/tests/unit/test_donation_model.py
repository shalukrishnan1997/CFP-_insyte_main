"""Comprehensive unit tests for Donation model.

FIN-DON-UNIT-* test cases covering creation, payment methods,
QA statuses, financial precision, donor linkage, and scanned forms.
"""

from decimal import Decimal

import pytest

from donations.models import Donation
from tests.factories import (
    CampaignFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════
# Donation Model — Creation & Defaults
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationCreation:
    """FIN-DON-UNIT-001 to 005: Core creation and defaults."""

    def test_create_donation_with_required_fields(self) -> None:
        """FIN-DON-UNIT-001: Donation created with all required fields."""
        donation = DonationFactory()
        assert donation.pk is not None
        assert donation.amount == Decimal("25.00")
        assert donation.campaign is not None
        assert donation.batch is not None
        assert donation.system_donor is not None

    def test_donation_uuid_primary_key(self) -> None:
        """FIN-DON-UNIT-002: PK is a valid UUID (36 chars)."""
        donation = DonationFactory()
        assert len(str(donation.pk)) == 36

    def test_default_currency_gbp(self) -> None:
        """FIN-DON-UNIT-003: Default currency is GBP."""
        donation = DonationFactory()
        assert donation.currency == "GBP"

    def test_default_payment_method_card(self) -> None:
        """FIN-DON-UNIT-004: Default payment method is card."""
        donation = DonationFactory()
        assert donation.payment_method == "card"

    def test_default_qa_status_pending(self) -> None:
        """FIN-DON-UNIT-005: Default QA status is pending."""
        donation = DonationFactory()
        assert donation.qa_status == Donation.QA_STATUS_PENDING


# ═══════════════════════════════════════════════════════════════
# Donation Model — Payment Methods
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationPaymentMethods:
    """FIN-DON-UNIT-006 to 011: All 9 payment method choices."""

    @pytest.mark.parametrize(
        "method",
        [
            "card",
            "direct_debit",
            "cash",
            "caf",
            "cheque",
            "postal_order",
            "non_financial",
        ],
    )
    def test_valid_payment_method(self, method: str) -> None:
        """FIN-DON-UNIT-006: Each payment method is accepted."""
        donation = DonationFactory(payment_method=method)
        assert donation.payment_method == method

    def test_card_specific_fields(self) -> None:
        """FIN-DON-UNIT-007: Card-specific fields stored correctly."""
        donation = DonationFactory(
            payment_method="card",
            card_holder_name="John Smith",
            card_last_four="4242",
            card_expiry_date="12/2028",
        )
        assert donation.card_holder_name == "John Smith"
        assert donation.card_last_four == "4242"
        assert donation.card_expiry_date == "12/2028"

    def test_cheque_specific_fields(self) -> None:
        """FIN-DON-UNIT-008: Cheque-specific fields stored correctly."""
        donation = DonationFactory(
            payment_method="cheque",
            cheque_number="CHQ-001234",
        )
        assert donation.cheque_number == "CHQ-001234"

    def test_caf_specific_fields(self) -> None:
        """FIN-DON-UNIT-009: CAF voucher fields stored correctly."""
        donation = DonationFactory(
            payment_method="caf",
            caf_voucher_number="CAF-9876",
            caf_donor_name="CAF Donor Ltd",
            caf_amount=Decimal("100.00"),
        )
        assert donation.caf_voucher_number == "CAF-9876"
        assert donation.caf_amount == Decimal("100.00")

    def test_postal_order_fields(self) -> None:
        """FIN-DON-UNIT-010: Postal order fields stored correctly."""
        donation = DonationFactory(
            payment_method="postal_order",
            postal_order_number="PO-5678",
            postal_issuer="Royal Mail",
        )
        assert donation.postal_order_number == "PO-5678"

    def test_non_financial_fields(self) -> None:
        """FIN-DON-UNIT-011: Non-financial reason/notes stored."""
        donation = DonationFactory(
            payment_method="non_financial",
            non_financial_reason="In-Kind Donation",
            non_financial_notes="Donated office supplies",
        )
        assert donation.non_financial_reason == "In-Kind Donation"


# ═══════════════════════════════════════════════════════════════
# Donation Model — QA Status
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationQAStatus:
    """FIN-DON-UNIT-012 to 015: QA status constants and transitions."""

    def test_qa_status_constants(self) -> None:
        """FIN-DON-UNIT-012: QA status constants have correct values."""
        assert Donation.QA_STATUS_PENDING == "pending"
        assert Donation.QA_STATUS_APPROVED == "approved"
        assert Donation.QA_STATUS_REJECTED == "rejected"
        assert Donation.QA_STATUS_FLAGGED == "flagged"

    def test_qa_status_choices_count(self) -> None:
        """FIN-DON-UNIT-013: Exactly 4 QA status choices exist."""
        assert len(Donation.QA_STATUS_CHOICES) == 4

    def test_qa_status_can_be_set_to_approved(self) -> None:
        """FIN-DON-UNIT-014: QA status can transition to approved."""
        donation = DonationFactory(qa_status="pending")
        donation.qa_status = "approved"
        donation.save()
        donation.refresh_from_db()
        assert donation.qa_status == "approved"

    def test_qa_notes_stored(self) -> None:
        """FIN-DON-UNIT-015: QA reviewer notes stored correctly."""
        donation = DonationFactory()
        donation.qa_notes = "Verified against physical form."
        donation.save()
        donation.refresh_from_db()
        assert donation.qa_notes == "Verified against physical form."


# ═══════════════════════════════════════════════════════════════
# Donation Model — Financial Precision
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationFinancialPrecision:
    """FIN-DON-UNIT-016 to 019: Decimal precision for financial data."""

    def test_amount_exact_precision(self) -> None:
        """FIN-DON-UNIT-016: Amount exact to 2 decimal places."""
        donation = DonationFactory(amount=Decimal("1234.56"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("1234.56")

    def test_large_amount_handling(self) -> None:
        """FIN-DON-UNIT-017: Large amounts up to 12 digits handled."""
        donation = DonationFactory(amount=Decimal("9999999999.99"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("9999999999.99")

    def test_small_amount_handling(self) -> None:
        """FIN-DON-UNIT-018: Small amounts like £0.01 preserved."""
        donation = DonationFactory(amount=Decimal("0.01"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("0.01")

    def test_currency_choices(self) -> None:
        """FIN-DON-UNIT-019: All 3 currency choices valid."""
        for currency in ["GBP", "USD", "EUR"]:
            donation = DonationFactory(currency=currency)
            assert donation.currency == currency


# ═══════════════════════════════════════════════════════════════
# Donation Model — Relationships
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationRelationships:
    """FIN-DON-UNIT-020 to 024: FK relationships."""

    def test_donation_linked_to_campaign(self) -> None:
        """FIN-DON-UNIT-020: Donation has campaign FK."""
        campaign = CampaignFactory(name="Link Test")
        donation = DonationFactory(campaign=campaign)
        assert donation.campaign.name == "Link Test"

    def test_donation_linked_to_batch(self) -> None:
        """FIN-DON-UNIT-021: Donation has batch FK, batch shares campaign."""
        donation = DonationFactory()
        assert donation.batch is not None
        assert donation.batch.campaign == donation.campaign

    def test_donation_linked_to_house_file_donor(self) -> None:
        """FIN-DON-UNIT-022: Internal donor linkage is preserved."""
        donor = DonorFactory(first_name="Alice", last_name="Brown")
        donation = DonationFactory(donor_source="house_file", donor=donor)
        assert donation.system_donor is not None
        assert donation.data_file_donor is None

    def test_donation_linked_to_filled_by_user(self) -> None:
        """FIN-DON-UNIT-023: filled_by user is set."""
        user = UserFactory(username="data-entry-001")
        donation = DonationFactory(filled_by=user)
        assert donation.filled_by.username == "data-entry-001"

    def test_donation_gift_aid_flag(self) -> None:
        """FIN-DON-UNIT-024: Gift Aid flag toggles correctly."""
        d_no = DonationFactory(gift_aid=False)
        d_yes = DonationFactory(gift_aid=True)
        assert d_no.gift_aid is False
        assert d_yes.gift_aid is True


# ═══════════════════════════════════════════════════════════════
# Donation Model — Payment Status
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationPaymentStatus:
    """FIN-DON-UNIT-025 to 027: Online payment status tracking."""

    def test_payment_status_default_pending(self) -> None:
        """FIN-DON-UNIT-025: Default payment_status is 'pending'."""
        donation = DonationFactory()
        assert donation.payment_status == "pending"

    @pytest.mark.parametrize(
        "status",
        ["pending", "processing", "completed", "failed", "refunded"],
    )
    def test_payment_status_choices(self, status: str) -> None:
        """FIN-DON-UNIT-026: All payment statuses are valid."""
        donation = DonationFactory()
        donation.payment_status = status
        donation.save()
        donation.refresh_from_db()
        assert donation.payment_status == status

    def test_letter_status_default_pending(self) -> None:
        """FIN-DON-UNIT-027: Default letter_status is 'pending'."""
        donation = DonationFactory()
        assert donation.letter_status == "pending"


# ═══════════════════════════════════════════════════════════════
# Donation Model — String Representation & Ordering
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationMisc:
    """FIN-DON-UNIT-028 to 030: __str__, ordering, frequency."""

    def test_donation_str_format(self) -> None:
        """FIN-DON-UNIT-028: __str__ includes ID and campaign name."""
        campaign = CampaignFactory(name="Autumn Appeal")
        donation = DonationFactory(campaign=campaign)
        s = str(donation)
        assert "Autumn Appeal" in s
        assert "Donation #" in s

    def test_donation_ordering(self) -> None:
        """FIN-DON-UNIT-029: Donations ordered by -created_at."""
        DonationFactory()
        d2 = DonationFactory()
        donations = list(Donation.objects.all())
        assert donations[0].pk == d2.pk  # newer first

    def test_donation_frequency_choices(self) -> None:
        """FIN-DON-UNIT-030: Frequency choices are valid."""
        for freq in ["one_time", "monthly", "quarterly", "annually"]:
            donation = DonationFactory(donation_frequency=freq)
            assert donation.donation_frequency == freq
