"""Service-layer tests for phone-intake non-financial donor updates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from audit.models import AuditLog
from campaigns.models import CampaignDataFile
from core.services.phone import normalize_phone
from donations.intake import (
    PhoneDonationPayload,
    create_phone_intake_batch,
    process_phone_non_financial_intake,
)
from donations.models import Donation
from donors.models import DataFileDonor
from donors.updates import DonorVanished
from tests.factories import (
    CampaignFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


def _payload(**overrides) -> PhoneDonationPayload:
    base: PhoneDonationPayload = {
        "amount": Decimal("0.00"),
        "currency": "GBP",
        "payment_method": Donation.PAYMENT_METHOD_NON_FINANCIAL,
        "donation_date": date(2026, 5, 7),
        "gift_aid": False,
        "donation_frequency": "",
        "donor_source": "house_file",
        "donor_match_status": "exact",
        "non_financial_reason": "legacy",
        "non_financial_notes": "Donor confirmed legacy intentions on call.",
    }
    base.update(overrides)
    return base


def _donor_update(**overrides) -> dict[str, object]:
    base: dict[str, object] = {
        "donor_first_name": "Jane",
        "donor_last_name": "Smith",
        "donor_email": "jane.smith@example.com",
        "donor_phone": "07700 999111",
        "donor_postcode": "SW1A 1AA",
        "donor_opt_in_phone": "on",
    }
    base.update(overrides)
    return base


def _setup(donor_factory=DonorFactory):
    operator = UserFactory()
    campaign = CampaignFactory()
    donor = donor_factory(client=campaign.client)
    batch = create_phone_intake_batch(operator=operator, campaign=campaign)
    return operator, campaign, donor, batch


def _create_data_file_donor(*, campaign, user) -> DataFileDonor:
    data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
    return DataFileDonor.objects.create(
        data_file=data_file,
        client=campaign.client,
        urn="DF001",
        title="Mr",
        first_name="Original",
        last_name="Donor",
        email="orig@example.com",
        phone="07700 000000",
        address_line1="1 Old Street",
        city="London",
        postcode="EC1Y 1AA",
        country="United Kingdom",
    )


@pytest.mark.django_db()
class TestPhoneNonFinancialOrchestrator:
    def test_mutates_linked_donor_and_creates_donation(self) -> None:
        operator, campaign, donor, batch = _setup()
        original_first = donor.first_name

        donation = process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(donor_first_name="Janet"),
        )

        donor.refresh_from_db()
        assert donor.first_name == "Janet"
        assert donor.first_name != original_first
        assert donation.payment_method == Donation.PAYMENT_METHOD_NON_FINANCIAL
        assert donation.amount == Decimal("0.00")
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.non_financial_reason == "legacy"
        assert donation.non_financial_notes.startswith("Donor confirmed")

    def test_letter_pipeline_skipped(self) -> None:
        operator, campaign, donor, batch = _setup()

        donation = process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(),
        )

        assert donation.letter_status == "skipped"
        assert donation.letter_void_reason == "Non-financial donor update"
        assert donation.letter_voided_at is not None

    def test_gift_aid_forced_false_even_when_payload_says_true(self) -> None:
        operator, campaign, donor, batch = _setup()

        donation = process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(gift_aid=True),
            donor_update_payload=_donor_update(),
        )

        assert donation.gift_aid is False

    def test_field_data_stamps_intake_subtype(self) -> None:
        operator, campaign, donor, batch = _setup()

        donation = process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(),
        )

        assert donation.field_data["intake_subtype"] == "donor_update"
        assert donation.field_data["intake_method"] == "phone"

    def test_contact_status_deceased_stamps_changed_at(self) -> None:
        operator, campaign, donor, batch = _setup()

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(
                donor_contact_status="deceased",
                donor_contact_status_reason="Family informed us",
            ),
        )

        donor.refresh_from_db()
        assert donor.contact_status == "deceased"
        assert donor.contact_status_reason == "Family informed us"
        assert donor.contact_status_changed_at is not None

    def test_works_for_donor_house_file(self) -> None:
        operator, campaign, donor, batch = _setup(donor_factory=DonorFactory)

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(donor_first_name="Updated"),
        )

        donor.refresh_from_db()
        assert donor.first_name == "Updated"

    def test_works_for_system_donor(self) -> None:
        operator, campaign, donor, batch = _setup(donor_factory=SystemDonorFactory)

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(donor_first_name="SysUpdated"),
        )

        donor.refresh_from_db()
        assert donor.first_name == "SysUpdated"

    def test_works_for_data_file_donor(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory(donor_source="data_file")
        donor = _create_data_file_donor(campaign=campaign, user=operator)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(donor_source="data_file"),
            donor_update_payload=_donor_update(donor_first_name="DFUpdated"),
        )

        donor.refresh_from_db()
        assert donor.first_name == "DFUpdated"

    def test_systemdonor_phone_change_updates_normalized_phone(self) -> None:
        operator, campaign, donor, batch = _setup(donor_factory=SystemDonorFactory)
        new_phone = "07700 999111"

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(donor_phone=new_phone),
        )

        donor.refresh_from_db()
        assert donor.phone == new_phone
        assert donor.normalized_phone == normalize_phone(new_phone)

    def test_no_donor_selected_raises(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        with pytest.raises(ValidationError):
            process_phone_non_financial_intake(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=None,  # type: ignore[arg-type]
                donation_payload=_payload(),
                donor_update_payload=_donor_update(),
            )

    def test_payment_method_not_non_financial_raises(self) -> None:
        operator, campaign, donor, batch = _setup()

        with pytest.raises(ValidationError):
            process_phone_non_financial_intake(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                donation_payload=_payload(
                    payment_method=Donation.PAYMENT_METHOD_CHEQUE
                ),
                donor_update_payload=_donor_update(),
            )

    def test_blank_reason_raises(self) -> None:
        operator, campaign, donor, batch = _setup()

        with pytest.raises(ValidationError):
            process_phone_non_financial_intake(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                donation_payload=_payload(non_financial_reason=""),
                donor_update_payload=_donor_update(),
            )

    def test_donor_vanished_raises_when_row_deleted_before_submit(self) -> None:
        operator, campaign, donor, batch = _setup()
        # Simulate another process deleting the donor between page-load and
        # submit; the orchestrator's row-locked re-fetch must surface this.
        original_pk = donor.pk
        type(donor).objects.filter(pk=original_pk).delete()

        with pytest.raises(DonorVanished):
            process_phone_non_financial_intake(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                donation_payload=_payload(),
                donor_update_payload=_donor_update(),
            )

    def test_audit_log_records_donor_change(self) -> None:
        operator, campaign, donor, batch = _setup()
        before = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"]
        ).count()

        process_phone_non_financial_intake(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            donation_payload=_payload(),
            donor_update_payload=_donor_update(donor_first_name="Audited"),
        )

        after = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"]
        ).count()
        assert after > before
