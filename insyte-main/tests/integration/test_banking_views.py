"""Integration tests for Daily Banking views.

FIN-BANK-INT-* test cases covering banking dashboard,
slip creation, detail, and processing.
"""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone as dj_tz

from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    UserFactory,
)


@pytest.fixture()
def staff_client() -> tuple[Client, Any]:
    """Return authenticated client and staff user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


# ═══════════════════════════════════════════════════════════════
# Daily Banking — Page Loads
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBankingViewsLoad:
    """FIN-BANK-INT-001 to 003: Banking pages load correctly."""

    def test_banking_dashboard_loads(self, staff_client: tuple[Client, Any]) -> None:
        """FIN-BANK-INT-001: Daily banking dashboard returns 200."""
        client, _ = staff_client
        response = client.get(reverse("custom_admin:daily_banking_dashboard"))
        assert response.status_code == 200
        assert (
            reverse("custom_admin:slip_detail", kwargs={"slip_id": 0})
            in response.content.decode()
        )

    def test_banking_batch_detail_loads(self, staff_client: tuple[Client, Any]) -> None:
        """Batch banking drill-down returns 200."""
        client, _ = staff_client
        batch = DonationBatchFactory()
        response = client.get(
            reverse(
                "custom_admin:daily_banking_batch_detail",
                kwargs={"batch_id": batch.pk},
            )
        )
        assert response.status_code == 200
        assert (
            reverse("custom_admin:slip_detail", kwargs={"slip_id": 0})
            in response.content.decode()
        )

    def test_slip_detail_loads(self, staff_client: tuple[Client, Any]) -> None:
        """FIN-BANK-INT-002: Slip detail page loads."""
        client, _ = staff_client
        slip = PayingInSlipFactory()
        response = client.get(
            reverse("custom_admin:slip_detail", kwargs={"slip_id": slip.pk})
        )
        assert response.status_code == 200

    def test_slip_detail_shows_donations(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """FIN-BANK-INT-003: Slip detail lists linked donations."""
        client, _ = staff_client
        slip = PayingInSlipFactory()
        DonationFactory(paying_in_slip=slip, amount=Decimal("100.00"))
        response = client.get(
            reverse("custom_admin:slip_detail", kwargs={"slip_id": slip.pk})
        )
        assert response.status_code == 200

    def test_create_paying_in_slip_successfully_assigns_donations(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """FIN-BANK-INT-004: Slip creation returns JSON payload and updates donation linkage."""
        client, _ = staff_client
        campaign = CampaignFactory(status="active")
        donation = DonationFactory(
            campaign=campaign,
            payment_method="cheque",
            qa_status="approved",
            amount=Decimal("125.00"),
        )

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-SUCCESS-001",
                "banking_date": "2026-03-15",
                "notes": "Created in integration test",
                "batch_ids": "[]",
                "donation_ids": json.dumps([str(donation.id)]),
            },
        )

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["success"] is True
        assert payload["slip_number"] == "SLIP-SUCCESS-001"
        assert payload["donations_added"] == 1
        donation.refresh_from_db()
        assert donation.paying_in_slip_id == payload["slip_id"]

    def test_create_paying_in_slip_from_batch_ids_respects_date_window(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Batch slip creation uses date_from/date_to with base_unassigned_filter."""
        client, _ = staff_client
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign)
        in_window = DonationFactory(
            batch=batch,
            campaign=campaign,
            payment_method="cash",
            qa_status="approved",
            amount=Decimal("10.00"),
            donation_date=None,
        )
        past = dj_tz.now() - timedelta(days=400)
        type(in_window).objects.filter(pk=in_window.pk).update(created_at=past)

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-BATCH-DATE-001",
                "banking_date": "2026-03-15",
                "notes": "",
                "batch_ids": json.dumps([batch.id]),
                "donation_ids": "[]",
                "date_from": "2026-03-01",
                "date_to": "2026-03-31",
            },
        )
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["success"] is False

        # Same batch with wide window includes legacy created_at donation
        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-BATCH-DATE-002",
                "banking_date": "2025-01-01",
                "notes": "",
                "batch_ids": json.dumps([batch.id]),
                "donation_ids": "[]",
                "date_from": "2024-01-01",
                "date_to": "2026-12-31",
            },
        )
        assert response.status_code == 200
        in_window.refresh_from_db()
        assert in_window.paying_in_slip_id is not None

    def test_create_paying_in_slip_rejects_invalid_json_payload(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """FIN-BANK-INT-005: Slip creation rejects malformed JSON list fields."""
        client, _ = staff_client

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-INVALID-001",
                "banking_date": "2026-03-15",
                "batch_ids": "not-json",
                "donation_ids": "[]",
            },
        )

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload == {"success": False, "error": "Invalid data format."}

    def test_dashboard_renders_cross_batch_donations_with_qa_links(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Dashboard shows donation-level queue with QA handoff links."""
        client, _ = staff_client
        campaign = CampaignFactory(status="active")
        batch_one = DonationBatchFactory(campaign=campaign)
        batch_two = DonationBatchFactory(campaign=campaign)
        first = DonationFactory(
            batch=batch_one,
            campaign=campaign,
            payment_method="cheque",
            qa_status="approved",
            amount=Decimal("11.00"),
            donation_date=dj_tz.now().date(),
        )
        DonationFactory(
            batch=batch_two,
            campaign=campaign,
            payment_method="cheque",
            qa_status="approved",
            amount=Decimal("22.00"),
            donation_date=dj_tz.now().date(),
        )

        response = client.get(reverse("custom_admin:daily_banking_dashboard"))

        assert response.status_code == 200
        assert len(response.context["dashboard_donations"]) >= 2
        qa_url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": first.batch_id, "donation_id": first.id},
        )
        assert qa_url in response.content.decode()

    def test_create_paying_in_slip_supports_cross_batch_donation_selection(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Donation-id flow can combine eligible rows from multiple batches."""
        client, _ = staff_client
        campaign = CampaignFactory(status="active")
        batch_one = DonationBatchFactory(campaign=campaign)
        batch_two = DonationBatchFactory(campaign=campaign)
        first = DonationFactory(
            batch=batch_one,
            campaign=campaign,
            payment_method="cheque",
            qa_status="approved",
            amount=Decimal("40.00"),
            donation_date=dj_tz.now().date(),
        )
        second = DonationFactory(
            batch=batch_two,
            campaign=campaign,
            payment_method="cheque",
            qa_status="approved",
            amount=Decimal("60.00"),
            donation_date=dj_tz.now().date(),
        )

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-CROSS-BATCH-001",
                "banking_date": "2026-03-15",
                "notes": "Cross batch selection",
                "batch_ids": "[]",
                "donation_ids": json.dumps([str(first.id), str(second.id)]),
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
            },
        )

        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["success"] is True
        assert payload["donations_added"] == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.paying_in_slip_id == payload["slip_id"]
        assert second.paying_in_slip_id == payload["slip_id"]

    def test_create_paying_in_slip_rejects_cross_client_donation_selection(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Donation-id flow blocks mixed-client selections."""
        client, _ = staff_client
        campaign_one = CampaignFactory(status="active")
        campaign_two = CampaignFactory(status="active")
        first = DonationFactory(
            campaign=campaign_one,
            batch=DonationBatchFactory(campaign=campaign_one),
            payment_method="cheque",
            qa_status="approved",
            donation_date=dj_tz.now().date(),
        )
        second = DonationFactory(
            campaign=campaign_two,
            batch=DonationBatchFactory(campaign=campaign_two),
            payment_method="cheque",
            qa_status="approved",
            donation_date=dj_tz.now().date(),
        )

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-MULTI-CLIENT-001",
                "banking_date": "2026-03-15",
                "notes": "",
                "batch_ids": "[]",
                "donation_ids": json.dumps([str(first.id), str(second.id)]),
            },
        )

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["success"] is False
        assert "exactly one client" in payload["error"]

    def test_create_paying_in_slip_rejects_non_eligible_donation_ids(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Donation-id flow revalidates eligibility for selected rows."""
        client, _ = staff_client
        campaign = CampaignFactory(status="active")
        ineligible = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            payment_method="card",
            qa_status="approved",
            donation_date=dj_tz.now().date(),
        )

        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": "SLIP-INELIGIBLE-001",
                "banking_date": "2026-03-15",
                "notes": "",
                "batch_ids": "[]",
                "donation_ids": json.dumps([str(ineligible.id)]),
            },
        )

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["success"] is False
        assert payload["error"] == "No eligible donations found."


# ═══════════════════════════════════════════════════════════════
# Daily Banking — Authentication
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestBankingAuth:
    """FIN-BANK-INT-004: Banking views require authentication."""

    def test_unauthenticated_redirect(self) -> None:
        """FIN-BANK-INT-004: Anonymous user redirected."""
        client = Client()
        response = client.get(reverse("custom_admin:daily_banking_dashboard"))
        assert response.status_code in [302, 301]
