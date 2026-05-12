"""End-to-end: phone DD intake → QA approve → letter eligibility.

Direct-debit phone donations are *capture-only* in this codebase: bank details
+ mandate consent are recorded, but no Stripe / GoCardless call is made.
After QA approval the donation must:

* End at ``payment_status="pending"`` (still no charge — banking will follow up
  in a future feature).
* Appear in ``build_letter_generation_queryset`` so the print console can
  send the donor a thank-you / confirmation letter.

DD donations land at ``qa_status=flagged`` until the BACS modulus check ships
(format-only validation today). The test confirms a reviewer can move that
flagged donation through QA without involving Stripe.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from core.models import User
from donations.models import Donation, DonationBatch
from letters.tasks import build_letter_generation_queryset
from tests.factories import (
    CampaignFactory,
    SystemDonorFactory,
    UserFactory,
)


@pytest.fixture()
def staff_client() -> tuple[Client, User]:
    user = UserFactory(is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db()
class TestPhoneIntakeDirectDebitEndToEnd:
    """Capture-only DD: intake records consent + bank details, never charges."""

    def test_dd_donation_through_qa_to_letter_queryset(
        self, staff_client: tuple[Client, User]
    ) -> None:
        http, _user = staff_client
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        # 1. Operator records the DD via the JSON endpoint.
        body = {
            "campaign_id": str(campaign.id),
            "amount": "20.00",
            "currency": "GBP",
            "payment_method": "direct_debit",
            "donation_frequency": "monthly",
            "donation_date": date(2026, 5, 2).isoformat(),
            "system_donor_id": str(donor.id),
            "donor_source": "house_file",
            "card_holder_name": "Jane Doe",
            "sort_code": "60-83-71",
            "account_number": "12345678",
            "direct_debit_start_date": "2026-06-01",
            "dd_mandate_consent": {"verbatim_read_aloud": True},
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        donation_id = resp.json()["donation_id"]
        donation = Donation.objects.get(pk=donation_id)

        # Phase 3 contract: DD lands at flagged + pending payment, encrypted
        # bank fields round-trip, mandate consent persisted.
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING
        assert donation.sort_code == "608371"
        assert donation.account_number == "12345678"
        assert donation.field_data["dd_mandate_consent"]["verbatim_read_aloud"] is True

        # 2. Reviewer approves the donation directly (the flagged-DD path
        #    requires explicit per-donation review, not bulk batch approve).
        donation.qa_status = Donation.QA_STATUS_APPROVED
        donation.save(update_fields=["qa_status"])

        batch = donation.batch
        assert batch is not None
        # Batch approve closes out the day for letter generation.
        batch.status = DonationBatch.STATUS_APPROVED
        batch.save(update_fields=["status"])

        donation.refresh_from_db()
        # Critical: payment_status stays at pending — no Stripe call ever
        # fires for capture-only DD.
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING

        # 3. The DD donation is eligible for letter generation.
        eligible = build_letter_generation_queryset(
            campaign=campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = {str(pk) for pk in eligible.values_list("id", flat=True)}
        assert str(donation.id) in eligible_ids

    def test_dd_without_consent_returns_400_and_no_donation_persisted(
        self, staff_client: tuple[Client, User]
    ) -> None:
        """Mandate consent is non-negotiable — server rejects, no row written."""
        http, _user = staff_client
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = {
            "campaign_id": str(campaign.id),
            "amount": "20.00",
            "payment_method": "direct_debit",
            "donation_frequency": "monthly",
            "system_donor_id": str(donor.id),
            "sort_code": "60-83-71",
            "account_number": "12345678",
            # No dd_mandate_consent — must be rejected.
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400
        # No donation should have been persisted by the rejected request.
        assert Donation.objects.filter(campaign=campaign, amount="20.00").count() == 0
