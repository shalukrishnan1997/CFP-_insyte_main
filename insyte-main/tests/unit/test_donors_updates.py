"""Unit tests for the lifted donor-mutation module ``donors.updates``."""

from __future__ import annotations

from datetime import date

import pytest

from campaigns.models import CampaignDataFile
from core.services.phone import normalize_phone
from donors.models import DataFileDonor
from donors.updates import (
    PHONE_INTAKE_EDITABLE_FIELDS,
    apply_donor_contact_status,
    apply_donor_field_updates,
)
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestApplyDonorFieldUpdates:
    """Full editable surface used by the phone-intake operator console."""

    def test_full_surface_phone_intake_set(self) -> None:
        donor = SystemDonorFactory(
            first_name="Old",
            last_name="Name",
            phone="07700 000000",
        )
        data = {
            "donor_first_name": "New",
            "donor_last_name": "Surname",
            "donor_email": "new@example.com",
            "donor_phone": "07700 999111",
            "donor_address_line1": "1 New Street",
            "donor_city": "Bristol",
            "donor_postcode": "BS1 1AA",
            "donor_country": "United Kingdom",
            "donor_date_of_birth": "1990-01-01",
            "donor_age": "34",
            "donor_gift_aid_declaration": "on",
            "donor_gift_aid_date": "2026-04-01",
            "donor_consent_contact": "on",
            "donor_opt_in_email": "on",
            "donor_contact_status": "deceased",
            "donor_contact_status_reason": "Family informed us",
        }

        updated = apply_donor_field_updates(
            donor, data, editable_fields=PHONE_INTAKE_EDITABLE_FIELDS
        )

        donor.refresh_from_db()
        assert donor.first_name == "New"
        assert donor.last_name == "Surname"
        assert donor.email == "new@example.com"
        assert donor.phone == "07700 999111"
        assert donor.normalized_phone == normalize_phone("07700 999111")
        assert donor.date_of_birth == date(1990, 1, 1)
        assert donor.age == 34
        assert donor.gift_aid_declaration is True
        assert donor.gift_aid_date == date(2026, 4, 1)
        assert donor.consent_contact is True
        assert donor.opt_in_email is True
        assert donor.contact_status == "deceased"
        assert donor.contact_status_reason == "Family informed us"
        assert donor.contact_status_changed_at is not None
        assert "first_name" in updated
        assert "normalized_phone" in updated  # auto-included for phone changes
        assert "contact_status" in updated

    def test_qa_default_set_omits_extended_fields(self) -> None:
        """``editable_fields=None`` preserves QA semantics: it does not touch
        ``consent_contact`` / ``date_of_birth`` / ``gift_aid_declaration``."""
        donor = SystemDonorFactory(consent_contact=False, gift_aid_declaration=False)
        data = {
            "donor_first_name": "Renamed",
            "donor_consent_contact": "on",
            "donor_gift_aid_declaration": "on",
            "donor_age": "55",
        }

        apply_donor_field_updates(donor, data, editable_fields=None)
        donor.refresh_from_db()

        assert donor.first_name == "Renamed"
        assert donor.consent_contact is False  # not touched
        assert donor.gift_aid_declaration is False  # not touched
        assert donor.age is None  # not touched

    def test_partial_update_does_not_clear_unsupplied_urn(self) -> None:
        """Missing ``donor_urn`` key must not blow away an existing URN
        (especially relevant for DataFileDonor, where ``urn`` is NOT NULL)."""
        donor = SystemDonorFactory(external_urn="EXISTING-URN")
        # No ``donor_urn`` key in payload.
        apply_donor_field_updates(
            donor,
            {"donor_first_name": "Charlie"},
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS,
        )

        donor.refresh_from_db()
        assert donor.external_urn == "EXISTING-URN"

    def test_systemdonor_phone_change_updates_normalized_phone_via_save(self) -> None:
        donor = SystemDonorFactory(phone="07700 000000")
        original_normalized = donor.normalized_phone

        apply_donor_field_updates(
            donor,
            {"donor_phone": "07700 999111"},
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS,
        )

        donor.refresh_from_db()
        assert donor.phone == "07700 999111"
        assert donor.normalized_phone != original_normalized
        assert donor.normalized_phone == normalize_phone("07700 999111")

    def test_blank_urn_does_not_clear_existing_data_file_donor_urn(self) -> None:
        """``DataFileDonor.urn`` is NOT NULL — submitting a blank URN must be
        treated as "no change" rather than crashing on save."""
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="EXISTING-URN",
            first_name="Original",
            last_name="Donor",
        )

        # Blank donor_urn — historically would attempt to clear it to None,
        # which crashes with IntegrityError. Now treated as "no change".
        apply_donor_field_updates(
            donor,
            {"donor_urn": "", "donor_first_name": "Renamed"},
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS,
        )

        donor.refresh_from_db()
        assert donor.urn == "EXISTING-URN"
        assert donor.first_name == "Renamed"


@pytest.mark.django_db()
class TestApplyDonorContactStatus:
    """Row-locked contact-status update."""

    def test_updates_each_linked_donor(self) -> None:
        client = ClientFactory()
        donor = DonorFactory(client=client)
        system_donor = SystemDonorFactory(client=client)

        from donors.models import Donor, SystemDonor

        apply_donor_contact_status(
            [(Donor, donor.pk), (SystemDonor, system_donor.pk)],
            contact_status="gone_away",
            reason="Returned to sender",
        )

        donor.refresh_from_db()
        system_donor.refresh_from_db()
        assert donor.contact_status == "gone_away"
        assert donor.contact_status_reason == "Returned to sender"
        assert donor.contact_status_changed_at is not None
        assert system_donor.contact_status == "gone_away"

    def test_invalid_status_raises(self) -> None:
        donor = DonorFactory()
        from donors.models import Donor

        with pytest.raises(ValueError):
            apply_donor_contact_status(
                [(Donor, donor.pk)],
                contact_status="bogus-status",
                reason="",
            )
