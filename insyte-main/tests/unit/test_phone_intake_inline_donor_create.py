"""Unit tests for inline SystemDonor creation during phone intake."""

import pytest

from donations.intake import create_system_donor_inline
from donors.models import SystemDonor
from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db()
class TestCreateSystemDonorInline:
    """Inline donor creation triggered when search yields no hits."""

    def test_creates_systemdonor_with_pending_review(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()

        donor = create_system_donor_inline(
            campaign=campaign,
            operator=operator,
            payload={
                "first_name": "Alice",
                "last_name": "Walker",
                "phone": "07700 900 123",
                "postcode": "SW1A 1AA",
                "email": "alice@example.com",
                "consent_contact": True,
                "opt_in_phone": True,
            },
        )

        assert isinstance(donor, SystemDonor)
        assert donor.first_name == "Alice"
        assert donor.last_name == "Walker"
        assert donor.pending_review is True
        assert donor.client_id == campaign.client.id
        assert donor.created_by_id == operator.id
        assert donor.consent_contact is True
        assert donor.opt_in_phone is True

    def test_normalized_phone_populated_from_save_override(self) -> None:
        """The SystemDonor.save() override should fill normalized_phone."""
        operator = UserFactory()
        campaign = CampaignFactory()

        donor = create_system_donor_inline(
            campaign=campaign,
            operator=operator,
            payload={
                "first_name": "Bob",
                "last_name": "Smith",
                "phone": "+44 7700 900 456",
            },
        )

        assert donor.normalized_phone == "07700900456"

    def test_missing_optional_fields_default_to_empty(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()

        donor = create_system_donor_inline(
            campaign=campaign,
            operator=operator,
            payload={"first_name": "Carol", "last_name": "Jones"},
        )

        assert donor.email == ""
        assert donor.phone == ""
        assert donor.normalized_phone == ""
        assert donor.postcode == ""
        assert donor.consent_contact is False

    def test_source_snapshot_marks_phone_intake_origin(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()

        donor = create_system_donor_inline(
            campaign=campaign,
            operator=operator,
            payload={"first_name": "Dan", "last_name": "Doe"},
        )

        assert donor.source_snapshot.get("intake_method") == "phone"
        assert "captured_at" in donor.source_snapshot
