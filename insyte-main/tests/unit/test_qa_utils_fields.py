from datetime import date

import pytest

from campaigns.models import CampaignDataFile
from custom_admin.views.qa_utils import (
    get_active_donor,
    update_donation_from_post,
    update_donor_from_post,
)
from donors.models import DataFileDonor
from tests.factories import (
    CampaignFactory,
    DonationFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


@pytest.mark.django_db
def test_update_donor_from_post_updates_extended_data_file_fields() -> None:
    user = UserFactory()
    campaign = CampaignFactory(donor_source="data_file")
    data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
    data_file_donor = DataFileDonor.objects.create(
        data_file=data_file,
        client=campaign.client,
        urn="URN001",
        first_name="Jane",
        last_name="Donor",
        country="United Kingdom",
        gift_aid_declaration=False,
        no_thank_you=False,
    )
    donation = DonationFactory(
        campaign=campaign,
        donor=None,
        donor_source="data_file",
        data_file_donor=data_file_donor,
    )

    updated = update_donor_from_post(
        donation,
        {
            "donor_urn": "URN001",
            "donor_title": "Ms",
            "donor_first_name": "Jane",
            "donor_last_name": "Donor",
            "donor_email": "",
            "donor_phone": "",
            "donor_address_line1": "",
            "donor_address_line2": "",
            "donor_city": "",
            "donor_county": "",
            "donor_postcode": "",
            "donor_country": "Ireland",
            "donor_no_thank_you": "on",
            "donor_opt_in_email": "on",
        },
    )

    data_file_donor.refresh_from_db()

    assert "country" in updated
    assert "no_thank_you" in updated
    assert data_file_donor.country == "Ireland"
    assert data_file_donor.gift_aid_declaration is False
    assert data_file_donor.no_thank_you is True
    assert data_file_donor.opt_in_email is True


@pytest.mark.django_db
def test_get_active_donor_falls_back_to_source_urn_when_system_urn_blank() -> None:
    user = UserFactory()
    campaign = CampaignFactory(donor_source="data_file")
    data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
    data_file_donor = DataFileDonor.objects.create(
        data_file=data_file,
        client=campaign.client,
        urn="URN-FROM-DATA-FILE",
        first_name="Jane",
        last_name="Donor",
    )
    system_donor = SystemDonorFactory(client=campaign.client, external_urn="")
    donation = DonationFactory(
        campaign=campaign,
        donor=None,
        donor_source="data_file",
        data_file_donor=data_file_donor,
        system_donor=system_donor,
    )

    active_donor = get_active_donor(donation)

    assert active_donor.urn == "URN-FROM-DATA-FILE"
    assert active_donor.first_name == system_donor.first_name


@pytest.mark.django_db
def test_get_active_donor_prefers_data_file_urn_when_house_file_urn_missing() -> None:
    user = UserFactory()
    campaign = CampaignFactory(donor_source="data_file")
    data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
    house_donor = DonorFactory(client=campaign.client, urn=None)
    data_file_donor = DataFileDonor.objects.create(
        data_file=data_file,
        client=campaign.client,
        urn="URN-FROM-MATCHED-DATA-FILE",
        first_name="Theo",
        last_name="Turner",
        house_file_donor=house_donor,
    )
    donation = DonationFactory(
        campaign=campaign,
        donor=house_donor,
        donor_source="data_file",
        data_file_donor=data_file_donor,
        system_donor=None,
    )

    active_donor = get_active_donor(donation)

    assert active_donor.urn == "URN-FROM-MATCHED-DATA-FILE"
    assert active_donor.first_name == house_donor.first_name


@pytest.mark.django_db
def test_update_donation_from_post_persists_posted_donation_date() -> None:
    donation = DonationFactory(donation_date=date(2024, 1, 1))

    updated = update_donation_from_post(
        donation,
        {
            "amount": "25.00",
            "currency": donation.currency,
            "payment_method": donation.payment_method,
            "donation_date": "2023-07-04",
        },
    )

    assert "donation_date" in updated
    assert donation.donation_date == date(2023, 7, 4)


@pytest.mark.django_db
def test_update_donation_from_post_keeps_existing_date_when_invalid() -> None:
    donation = DonationFactory(donation_date=date(2024, 1, 1))

    updated = update_donation_from_post(
        donation,
        {
            "amount": "25.00",
            "currency": donation.currency,
            "payment_method": donation.payment_method,
            "donation_date": "invalid-date",
        },
    )

    assert "donation_date" not in updated
    assert donation.donation_date == date(2024, 1, 1)


@pytest.mark.parametrize(
    ("payment_method", "payload", "expected"),
    [
        (
            "card",
            {
                "card_holder_name": "Jane Donor",
                "card_last_four": "12345678",
                "card_expiry_date": "12/2030",
            },
            {
                "card_holder_name": "Jane Donor",
                "card_last_four": "5678",
                "card_expiry_date": "12/2030",
            },
        ),
        (
            "direct_debit",
            {
                "direct_debit_start_date": "2026-01-01",
                "direct_debit_end_date": "2026-12-31",
            },
            {
                "direct_debit_start_date": date(2026, 1, 1),
                "direct_debit_end_date": date(2026, 12, 31),
            },
        ),
        (
            "cheque",
            {"cheque_number": "CHQ-1001", "cheque_date": "2026-03-15"},
            {"cheque_number": "CHQ-1001", "cheque_date": date(2026, 3, 15)},
        ),
        (
            "caf",
            {
                "caf_voucher_number": "CAF-7788",
                "caf_donor_name": "CAF Donor",
                "caf_amount": "88.20",
            },
            {
                "caf_voucher_number": "CAF-7788",
                "caf_donor_name": "CAF Donor",
                "caf_amount": "88.20",
            },
        ),
        (
            "postal_order",
            {
                "postal_order_number": "PO-900",
                "postal_order_date": "2026-04-02",
                "postal_issuer": "Post Office",
            },
            {
                "postal_order_number": "PO-900",
                "postal_order_date": date(2026, 4, 2),
                "postal_issuer": "Post Office",
            },
        ),
        (
            "non_financial",
            {
                "non_financial_reason": "in_kind",
                "non_financial_notes": "Donated supplies",
            },
            {
                "non_financial_reason": "in_kind",
                "non_financial_notes": "Donated supplies",
            },
        ),
        ("cash", {}, {}),
    ],
)
@pytest.mark.django_db
def test_update_donation_from_post_supports_all_payment_methods(
    payment_method: str, payload: dict[str, str], expected: dict[str, object]
) -> None:
    donation = DonationFactory(payment_method="cash")
    data = {
        "amount": "25.00",
        "currency": donation.currency,
        "payment_method": payment_method,
        "donation_date": "2026-03-27",
        **payload,
    }

    update_donation_from_post(donation, data)

    assert donation.payment_method == payment_method
    for field, value in expected.items():
        if isinstance(value, str) and field in {
            "caf_amount",
        }:
            assert str(getattr(donation, field)) == value
        else:
            assert getattr(donation, field) == value
