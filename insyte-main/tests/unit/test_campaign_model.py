"""Comprehensive unit tests for Campaign and CampaignField models.

FIN-CAMP-UNIT-* test cases covering creation, validation, properties,
status constants, date validation, and payment tracking fields.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from campaigns.models import Campaign, CampaignField, PackageCode
from tests.factories import CampaignFactory, ClientFactory, UserFactory

# ═══════════════════════════════════════════════════════════════
# Campaign Model — Core Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignCreation:
    """FIN-CAMP-UNIT-001 to 003: Campaign creation and field defaults."""

    def test_create_campaign_with_all_fields(self) -> None:
        """FIN-CAMP-UNIT-001: Campaign created with all fields populated."""
        client = ClientFactory()
        user = UserFactory()
        campaign = CampaignFactory(
            client=client,
            name="Winter Relief Fund 2026",
            description="Comprehensive test campaign",
            status="active",
            target_amount=Decimal("500000.00"),
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
            created_by=user,
        )
        assert campaign.pk is not None
        assert campaign.name == "Winter Relief Fund 2026"
        assert campaign.client == client
        assert campaign.status == "active"
        assert campaign.target_amount == Decimal("500000.00")

    def test_campaign_uuid_primary_key(self) -> None:
        """FIN-CAMP-UNIT-002: Campaign PK is a valid UUID."""
        campaign = CampaignFactory()
        assert len(str(campaign.pk)) == 36  # UUID format

    def test_campaign_default_status_is_draft(self) -> None:
        """FIN-CAMP-UNIT-003: Default status is 'draft'."""
        campaign = CampaignFactory()
        assert campaign.status == Campaign.STATUS_DRAFT


# ═══════════════════════════════════════════════════════════════
# Campaign Model — Status Constants & Properties
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignStatusProperties:
    """FIN-CAMP-UNIT-004 to 007: Status constants and property methods."""

    def test_status_constants_values(self) -> None:
        """FIN-CAMP-UNIT-004: Status constants have exact expected values."""
        assert Campaign.STATUS_DRAFT == "draft"
        assert Campaign.STATUS_ACTIVE == "active"
        assert Campaign.STATUS_LIVE == "active"  # Alias
        assert Campaign.STATUS_CLOSED == "closed"

    def test_campaign_temperature_constants_values(self) -> None:
        """FIN-CAMP-UNIT-004A: Campaign temperature constants are stable."""
        assert Campaign.CAMPAIGN_TEMPERATURE_WARM == "warm"
        assert Campaign.CAMPAIGN_TEMPERATURE_COLD == "cold"

    def test_is_active_true_only_for_active(self) -> None:
        """FIN-CAMP-UNIT-005: is_active returns True only for 'active' status."""
        assert CampaignFactory(status="draft").is_active is False
        assert CampaignFactory(status="active").is_active is True
        assert CampaignFactory(status="closed").is_active is False

    def test_is_closed_true_only_for_closed(self) -> None:
        """FIN-CAMP-UNIT-006: is_closed returns True only for 'closed' status."""
        assert CampaignFactory(status="draft").is_closed is False
        assert CampaignFactory(status="active").is_closed is False
        assert CampaignFactory(status="closed").is_closed is True

    def test_can_upload_donors_false_when_closed(self) -> None:
        """FIN-CAMP-UNIT-007: can_upload_donors returns False only for closed."""
        assert CampaignFactory(status="draft").can_upload_donors is True
        assert CampaignFactory(status="active").can_upload_donors is True
        assert CampaignFactory(status="closed").can_upload_donors is False


# ═══════════════════════════════════════════════════════════════
# Campaign Model — Validation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignValidation:
    """FIN-CAMP-UNIT-008 to 010: Date validation and clean()."""

    def test_clean_rejects_end_before_start(self) -> None:
        """FIN-CAMP-UNIT-008: clean() raises error when end_date < start_date."""
        campaign = CampaignFactory.build(
            start_date=date(2026, 3, 31),
            end_date=date(2026, 3, 1),
        )
        with pytest.raises(ValidationError):
            campaign.clean()

    def test_clean_accepts_valid_dates(self) -> None:
        """FIN-CAMP-UNIT-009: clean() passes when end_date > start_date."""
        campaign = CampaignFactory.build(
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
        )
        campaign.clean()  # Should not raise

    def test_clean_accepts_null_dates(self) -> None:
        """FIN-CAMP-UNIT-010: clean() passes when dates are None."""
        campaign = CampaignFactory.build(
            start_date=None,
            end_date=None,
            client=ClientFactory(),
        )
        campaign.clean()  # Should not raise

    def test_campaign_temperature_defaults_to_cold(self) -> None:
        """FIN-CAMP-UNIT-010A: Campaigns default to cold workflow."""
        campaign = CampaignFactory()
        assert campaign.campaign_temperature == Campaign.CAMPAIGN_TEMPERATURE_COLD

    def test_full_clean_rejects_missing_client(self) -> None:
        """Campaign validation should reject client-less campaign records."""
        campaign = CampaignFactory.build(client=None)
        with pytest.raises(ValidationError):
            campaign.full_clean()


# ═══════════════════════════════════════════════════════════════
# Campaign Model — Relationships & Misc
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignRelationships:
    """FIN-CAMP-UNIT-011 to 014: Client linkage, payment fields, __str__."""

    def test_campaign_linked_to_client(self) -> None:
        """FIN-CAMP-UNIT-011: Campaign has a valid client FK."""
        client = ClientFactory(name="Oxfam Test")
        campaign = CampaignFactory(client=client)
        assert campaign.client.name == "Oxfam Test"

    def test_campaign_str_includes_name_and_client(self) -> None:
        """FIN-CAMP-UNIT-012: __str__ returns 'name (client)'."""
        client = ClientFactory(name="CharityA")
        campaign = CampaignFactory(client=client, name="Spring Appeal")
        assert str(campaign) == "Spring Appeal (CharityA)"

    def test_campaign_ordering_by_created_at(self) -> None:
        """FIN-CAMP-UNIT-014: Campaigns ordered by -created_at."""
        CampaignFactory(name="First")
        CampaignFactory(name="Second")
        campaigns = list(Campaign.objects.all())
        assert campaigns[0].name == "Second"
        assert campaigns[1].name == "First"


# ═══════════════════════════════════════════════════════════════
# Campaign Model — Payment Processing Fields
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignPaymentFields:
    """FIN-CAMP-UNIT-015 to 018: Payment tracking field defaults."""

    def test_payment_eligible_default_false(self) -> None:
        """FIN-CAMP-UNIT-015: payment_eligible defaults to False."""
        campaign = CampaignFactory()
        assert campaign.payment_eligible is False

    def test_payment_status_default(self) -> None:
        """FIN-CAMP-UNIT-016: payment_status defaults to 'not_started'."""
        campaign = CampaignFactory()
        assert campaign.payment_status == "not_started"

    def test_total_payment_amount_default(self) -> None:
        """FIN-CAMP-UNIT-017: total_payment_amount defaults to 0."""
        campaign = CampaignFactory()
        assert campaign.total_payment_amount == 0

    def test_successful_failed_payment_defaults(self) -> None:
        """FIN-CAMP-UNIT-018: successful/failed payment counts default to 0."""
        campaign = CampaignFactory()
        assert campaign.successful_payments == 0
        assert campaign.failed_payments == 0


# ═══════════════════════════════════════════════════════════════
# Campaign Model — HGV/LGV
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignHgvLgv:
    """FIN-CAMP-UNIT-019: HGV/LGV amount precision."""

    def test_hgv_lgv_decimal_precision(self) -> None:
        """FIN-CAMP-UNIT-019: HGV/LGV store exact decimal values."""
        campaign = CampaignFactory(
            hgv_amount=Decimal("5000.50"),
            lgv_amount=Decimal("5.25"),
        )
        campaign.refresh_from_db()
        assert campaign.hgv_amount == Decimal("5000.50")
        assert campaign.lgv_amount == Decimal("5.25")


# ═══════════════════════════════════════════════════════════════
# PackageCode Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestPackageCode:
    """FIN-CAMP-UNIT-020 to 022: PackageCode model tests."""

    def test_create_package_code(self) -> None:
        """FIN-CAMP-UNIT-020: PackageCode can be created."""
        pkg = PackageCode.objects.create(code="PKG001", description="Test package")
        assert pkg.pk is not None
        assert pkg.is_active is True

    def test_package_code_unique(self) -> None:
        """FIN-CAMP-UNIT-021: Duplicate codes rejected."""
        PackageCode.objects.create(code="DUP001")
        with pytest.raises(IntegrityError):
            PackageCode.objects.create(code="DUP001")

    def test_package_code_str(self) -> None:
        """FIN-CAMP-UNIT-022: __str__ returns the code value."""
        pkg = PackageCode.objects.create(code="SPRING26")
        assert str(pkg) == "SPRING26"


# ═══════════════════════════════════════════════════════════════
# CampaignField Model
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignField:
    """FIN-CAMP-UNIT-023 to 026: CampaignField (dynamic form fields)."""

    def test_create_campaign_field(self) -> None:
        """FIN-CAMP-UNIT-023: CampaignField created with valid data."""
        campaign = CampaignFactory()
        field = CampaignField.objects.create(
            campaign=campaign,
            label="Donor Phone",
            field_type="phone",
            required=True,
        )
        assert field.pk is not None
        assert field.required is True

    def test_field_type_choices(self) -> None:
        """FIN-CAMP-UNIT-024: All field type constants are valid."""
        expected = [
            "text",
            "number",
            "email",
            "phone",
            "date",
            "dropdown",
            "radio",
            "checkbox",
            "textarea",
            "file",
        ]
        actual = [c[0] for c in CampaignField.FIELD_TYPE_CHOICES]
        assert actual == expected

    def test_unique_label_per_campaign(self) -> None:
        """FIN-CAMP-UNIT-025: Duplicate labels within same campaign rejected."""
        campaign = CampaignFactory()
        CampaignField.objects.create(campaign=campaign, label="Name")
        with pytest.raises(IntegrityError):
            CampaignField.objects.create(campaign=campaign, label="Name")

    def test_same_label_different_campaigns(self) -> None:
        """FIN-CAMP-UNIT-026: Same label allowed across different campaigns."""
        c1 = CampaignFactory()
        c2 = CampaignFactory()
        CampaignField.objects.create(campaign=c1, label="Name")
        f2 = CampaignField.objects.create(campaign=c2, label="Name")
        assert f2.pk is not None
