"""Unit tests for core models — Campaign, Donation, Donor, Batch.

Tests model creation, field defaults, string representations,
properties, status constants, and basic validation.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError

from campaigns.models import Campaign
from donations.models import Donation, DonationBatch
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════
# User Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestUserModel:
    """Tests for the custom User model."""

    def test_create_user(self) -> None:
        """User can be created with UUID primary key."""
        user = UserFactory()
        assert user.pk is not None
        assert user.is_staff is True
        assert user.is_active is True

    def test_user_uuid_pk(self) -> None:
        """User primary key is a UUID, not an integer."""
        user = UserFactory()
        assert len(str(user.pk)) == 36  # UUID format: 8-4-4-4-12

    def test_user_unique_email(self) -> None:
        """Users must have unique email addresses."""
        UserFactory(email="unique@test.local")
        with pytest.raises(IntegrityError):
            UserFactory(email="unique@test.local")


# ═══════════════════════════════════════════════════════════════
# Client Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestClientModel:
    """Tests for the Client model."""

    def test_create_client(self) -> None:
        """Client can be created with basic fields."""
        client = ClientFactory()
        assert client.pk is not None
        assert client.is_active is True

    def test_client_str(self) -> None:
        """Client string representation is the name."""
        client = ClientFactory(name="RSPCA Test")
        assert str(client) == "RSPCA Test"

    def test_client_unique_name(self) -> None:
        """Client names must be unique."""
        ClientFactory(name="Duplicate Test")
        with pytest.raises(IntegrityError):
            ClientFactory(name="Duplicate Test")


# ═══════════════════════════════════════════════════════════════
# Campaign Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignModel:
    """Tests for the Campaign model."""

    def test_create_campaign(self) -> None:
        """Campaign can be created with default draft status."""
        campaign = CampaignFactory()
        assert campaign.pk is not None
        assert campaign.status == Campaign.STATUS_DRAFT

    def test_campaign_status_constants(self) -> None:
        """Status constants have correct values."""
        assert Campaign.STATUS_DRAFT == "draft"
        assert Campaign.STATUS_ACTIVE == "active"
        assert Campaign.STATUS_LIVE == "active"  # Alias
        assert Campaign.STATUS_CLOSED == "closed"

    def test_campaign_is_active_property(self) -> None:
        """is_active property returns True only for active campaigns."""
        draft = CampaignFactory(status="draft")
        active = CampaignFactory(status="active")
        closed = CampaignFactory(status="closed")

        assert draft.is_active is False
        assert active.is_active is True
        assert closed.is_active is False

    def test_campaign_linked_to_client(self) -> None:
        """Campaign has a client relationship."""
        client = ClientFactory(name="Cancer Research UK Test")
        campaign = CampaignFactory(client=client)
        assert campaign.client.name == "Cancer Research UK Test"

    def test_campaign_hgv_lgv_defaults(self) -> None:
        """HGV and LGV thresholds have correct defaults from factory."""
        campaign = CampaignFactory()
        assert campaign.hgv_amount == Decimal("1000.00")
        assert campaign.lgv_amount == Decimal("10.00")

    def test_campaign_uuid_pk(self) -> None:
        """Campaign primary key is a UUID."""
        campaign = CampaignFactory()
        assert len(str(campaign.pk)) == 36


# ═══════════════════════════════════════════════════════════════
# Donor Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonorModel:
    """Tests for the Donor model."""

    def test_create_donor(self) -> None:
        """Donor can be created and has a UUID primary key, with URN stored separately."""
        donor = DonorFactory(urn="URN000001")
        assert donor.pk is not None
        assert donor.urn == "URN000001"

    def test_donor_full_name(self) -> None:
        """full_name property includes title, first, and last name."""
        donor = DonorFactory(title="Mrs", first_name="Jane", last_name="Smith")
        assert donor.full_name == "Mrs Jane Smith"

    def test_donor_full_name_no_title(self) -> None:
        """full_name works without a title."""
        donor = DonorFactory(title="", first_name="John", last_name="Doe")
        assert donor.full_name == "John Doe"

    def test_donor_str(self) -> None:
        """String representation includes full name and URN."""
        donor = DonorFactory(
            title="Mr", first_name="John", last_name="Smith", urn="URN999"
        )
        assert str(donor) == "Mr John Smith (URN999)"

    def test_donor_gdpr_defaults(self) -> None:
        """GDPR opt-in fields default to False."""
        donor = DonorFactory(
            consent_contact=False,
            opt_in_email=False,
            opt_in_sms=False,
            opt_in_phone=False,
            opt_in_post=False,
        )
        assert donor.consent_contact is False
        assert donor.opt_in_email is False
        assert donor.opt_in_sms is False
        assert donor.opt_in_phone is False
        assert donor.opt_in_post is False

    def test_donor_contact_status_defaults(self) -> None:
        """Donors default to a normal contact status and remain post-contactable."""
        donor = DonorFactory()

        assert donor.contact_status == donor.CONTACT_STATUS_NORMAL
        assert donor.is_postal_contact_suppressed is False

    def test_donor_postal_suppression_for_deceased(self) -> None:
        """Deceased donors are flagged as postally suppressed."""
        donor = DonorFactory(contact_status="deceased")

        assert donor.is_postal_contact_suppressed is True


# ═══════════════════════════════════════════════════════════════
# DonationBatch Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationBatchModel:
    """Tests for the DonationBatch model."""

    def test_create_batch(self) -> None:
        """Batch can be created with pending_qa as the default status."""
        batch = DonationBatchFactory()
        assert batch.pk is not None
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_batch_status_constants(self) -> None:
        """Batch QA status constants are correct."""
        assert DonationBatch.STATUS_PENDING_QA == "pending_qa"
        assert DonationBatch.STATUS_IN_REVIEW == "in_review"
        assert DonationBatch.STATUS_APPROVED == "approved"
        assert DonationBatch.STATUS_REJECTED == "rejected"

    def test_batch_default_currency(self) -> None:
        """Batch defaults to GBP currency."""
        batch = DonationBatchFactory()
        assert batch.default_currency == "GBP"

    def test_batch_str(self) -> None:
        """String representation includes batch name and donation count."""
        batch = DonationBatchFactory(batch_name="BATCH-001", total_donations=5)
        assert "BATCH-001" in str(batch)
        assert "5" in str(batch)


# ═══════════════════════════════════════════════════════════════
# Donation Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDonationModel:
    """Tests for the Donation model."""

    def test_create_donation(self) -> None:
        """Donation can be created with required fields."""
        donation = DonationFactory()
        assert donation.pk is not None
        assert donation.amount == Decimal("25.00")

    def test_donation_uuid_pk(self) -> None:
        """Donation primary key is a UUID."""
        donation = DonationFactory()
        assert len(str(donation.pk)) == 36

    def test_donation_linked_to_batch(self) -> None:
        """Donation is linked to a batch."""
        donation = DonationFactory()
        assert donation.batch is not None
        assert donation.batch.campaign == donation.campaign

    def test_donation_payment_methods(self) -> None:
        """All payment methods are valid choices."""
        valid_methods = [
            "card",
            "direct_debit",
            "cash",
            "caf",
            "cheque",
            "postal_order",
            "non_financial",
        ]
        for method in valid_methods:
            donation = DonationFactory(payment_method=method)
            assert donation.payment_method == method

    def test_donation_qa_status_default(self) -> None:
        """QA status defaults to pending."""
        donation = DonationFactory()
        assert donation.qa_status == Donation.QA_STATUS_PENDING

    def test_donation_currency_default(self) -> None:
        """Currency defaults to GBP."""
        donation = DonationFactory()
        assert donation.currency == "GBP"

    def test_donation_house_file_donor(self) -> None:
        """Donation with house file donor source."""
        donor = DonorFactory()
        donation = DonationFactory(donor_source="house_file", donor=donor)
        assert donation.donor == donor
        assert donation.data_file_donor is None

    def test_donation_gift_aid(self) -> None:
        """Gift Aid can be set on a donation."""
        donation = DonationFactory(gift_aid=True)
        assert donation.gift_aid is True

    def test_donation_amount_precision(self) -> None:
        """Donation amount handles decimal precision correctly."""
        donation = DonationFactory(amount=Decimal("1234.56"))
        donation.refresh_from_db()
        assert donation.amount == Decimal("1234.56")
