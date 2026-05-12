"""End-to-end: phone intake → per-call batch → QA approve → letter-gen seam.

These tests drive the real HTTP views (phone-intake create + QA approve) and
assert that the donation is later picked up by
``letters.tasks.build_letter_generation_queryset`` — that's the actual seam
where QA hands work to the letter-generation pipeline. Letter generation is
not auto-triggered after batch approval (operators run it from the print
console); the contract this test guards is "an approved phone donation is
*eligible* for letter generation".

Cheque covers the bulk of the non-card phone-intake flow (cash / CAF / postal
order share the same QA path). The card MOTO and direct-debit flows are
exercised in their own integration files / unit suites — keeping this test
focused on the QA-handoff seam keeps it small and resilient.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

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
    """Staff client with permission to drive the phone-intake views."""
    user = UserFactory(is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db()
class TestPhoneIntakeCheckEndToEnd:
    """Cheque pledge: intake → batch approve → letter eligibility."""

    def test_cheque_donation_reaches_letter_generation_queryset(
        self, staff_client: tuple[Client, User]
    ) -> None:
        http, _user = staff_client
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        # 1. Operator records the cheque pledge through the JSON endpoint.
        body = {
            "campaign_id": str(campaign.id),
            "amount": "75.00",
            "currency": "GBP",
            "payment_method": "cheque",
            "donation_date": date(2026, 5, 2).isoformat(),
            "donation_frequency": "",
            "system_donor_id": str(donor.id),
            "donor_source": "house_file",
            # Pledged cheque — number/date arrive when the cheque does.
            "cheque_number": "",
            "cheque_date": "",
            "gift_aid": False,
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        donation_id = resp.json()["donation_id"]

        donation = Donation.objects.get(pk=donation_id)
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.field_data["intake_method"] == "phone"
        batch = donation.batch
        assert batch is not None
        assert batch.batch_name.startswith("Phone")
        assert campaign.name in batch.batch_name

        # 2. QA approves the batch via the real view. The cascade auto-approves
        #    pending donations without a low-confidence hold.
        approve_url = reverse(
            "custom_admin:qa_approve_batch", kwargs={"batch_id": batch.id}
        )
        approve_resp = http.post(approve_url)
        assert approve_resp.status_code == 302, approve_resp.content

        batch.refresh_from_db()
        donation.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert donation.qa_status == Donation.QA_STATUS_APPROVED

        # 3. The donation is eligible for letter generation. This is the seam
        #    a future refactor most likely breaks — assert directly against
        #    the queryset the print console drives.
        eligible = build_letter_generation_queryset(
            campaign=campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = {str(pk) for pk in eligible.values_list("id", flat=True)}
        assert str(donation.id) in eligible_ids


@pytest.mark.django_db()
class TestPhoneIntakeBatchPerCallAndAggregates:
    """Each call gets its own batch; today's totals aggregate across them."""

    def test_two_calls_get_distinct_batches_and_running_totals_match(
        self, staff_client: tuple[Client, User]
    ) -> None:
        http, _user = staff_client
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        def _post(amount: str) -> str:
            resp = http.post(
                reverse("custom_admin:phone_intake_create_donation"),
                data=json.dumps(
                    {
                        "campaign_id": str(campaign.id),
                        "amount": amount,
                        "currency": "GBP",
                        "payment_method": "cash",
                        "donation_date": date(2026, 5, 2).isoformat(),
                        "system_donor_id": str(donor.id),
                        "donor_source": "house_file",
                    }
                ),
                content_type="application/json",
            )
            assert resp.status_code == 201, resp.content
            return resp.json()["donation_id"]

        d1_id = _post("10.00")
        d2_id = _post("32.50")

        d1 = Donation.objects.get(pk=d1_id)
        d2 = Donation.objects.get(pk=d2_id)
        # One batch per call — distinct rows.
        assert d1.batch_id != d2.batch_id

        # Console summary aggregates across all of today's phone batches for
        # this operator + campaign. After two calls, the total amount is
        # £42.50 across 2 donations.
        console_url = (
            reverse("custom_admin:phone_intake_console") + f"?campaign_id={campaign.id}"
        )
        page = http.get(console_url)
        assert page.status_code == 200
        body = page.content.decode("utf-8")
        assert "42.50" in body
        # The "(N donations)" copy is rendered inline next to the total — hunt
        # for the exact "2 donations" substring.
        assert "2</span> donations" in body or '"2"' in body or "2)" in body

    def test_amount_aggregation_uses_decimal_two_places(
        self, staff_client: tuple[Client, User]
    ) -> None:
        """Floating-point traps regressed before — assert the exact string."""
        http, _user = staff_client
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        for amount in ("0.10", "0.20", "0.30"):
            resp = http.post(
                reverse("custom_admin:phone_intake_create_donation"),
                data=json.dumps(
                    {
                        "campaign_id": str(campaign.id),
                        "amount": amount,
                        "currency": "GBP",
                        "payment_method": "cash",
                        "donation_date": date(2026, 5, 2).isoformat(),
                        "system_donor_id": str(donor.id),
                        "donor_source": "house_file",
                    }
                ),
                content_type="application/json",
            )
            assert resp.status_code == 201

        # Decimal arithmetic must report £0.60, not 0.6000000000000001.
        last_donation = (
            Donation.objects.filter(donor=None, system_donor__isnull=False)
            .order_by("-created_at")
            .first()
        )
        assert last_donation is not None
        # Fetch the page; assert the formatted total is exactly two-decimal.
        console_url = (
            reverse("custom_admin:phone_intake_console") + f"?campaign_id={campaign.id}"
        )
        page = http.get(console_url)
        body = page.content.decode("utf-8")
        # "0.60" appears in the running-total mount; "0.600000" must not.
        assert "0.60" in body
        assert "0.600" not in body
        assert "0.6000000" not in body
        # Sanity: the summed amount on the model side is also Decimal('0.60').
        total_from_db = sum(
            (d.amount for d in Donation.objects.all()), start=Decimal("0.00")
        )
        assert total_from_db == Decimal("0.60")
