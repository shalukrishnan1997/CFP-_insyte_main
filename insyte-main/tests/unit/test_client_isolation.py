"""Unit tests for client data isolation guardrails."""

import json
from datetime import date
from typing import Any

import pytest
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.test import RequestFactory

from banking.admin_views import create_paying_in_slip
from banking.admin_views_slips import slip_add_donations
from banking.models import PayingInSlip
from campaigns.models import Campaign
from client_portal.views import client_supporter_detail
from clients.models import ClientPortalUser
from custom_admin.api_views import donor_search
from donors.models import Donor
from invoices.admin_views import _handle_invoice_create_post, invoice_metrics_api
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestClientIsolationGuards:
    """Regression tests ensuring cross-client access/mixing is blocked."""

    def test_invoice_metrics_rejects_campaign_from_other_client(self) -> None:
        """Metrics API rejects campaign/client mismatches."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        selected_client = ClientFactory()
        foreign_campaign = CampaignFactory()

        request = RequestFactory().get(
            "/admin/invoices/api/metrics/",
            {
                "client_id": str(selected_client.id),
                "campaign_id": str(foreign_campaign.id),
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
        request.user = staff_user

        response = invoice_metrics_api(request)

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert "does not belong" in payload["error"]

    def test_create_paying_in_slip_rejects_mixed_client_donations(self) -> None:
        """Creating a slip with donations from multiple clients is rejected."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        campaign_one = CampaignFactory(status=Campaign.STATUS_ACTIVE)
        campaign_two = CampaignFactory(status=Campaign.STATUS_ACTIVE)

        donation_one = DonationFactory(
            campaign=campaign_one,
            payment_method="cheque",
            qa_status="approved",
        )
        donation_two = DonationFactory(
            campaign=campaign_two,
            payment_method="cash",
            qa_status="approved",
        )

        request = RequestFactory().post(
            "/admin/daily-banking/create-slip/",
            {
                "slip_number": "SLIP-MIXED-001",
                "banking_date": "2026-03-01",
                "batch_ids": "[]",
                "donation_ids": json.dumps(
                    [str(donation_one.id), str(donation_two.id)]
                ),
            },
        )
        request.user = staff_user

        response = create_paying_in_slip(request)

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert "exactly one client" in payload["error"]

    def test_slip_add_donations_rejects_different_client(self) -> None:
        """Adding donations from a different client to a slip is rejected."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        slip_client = ClientFactory()
        other_campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)

        slip = PayingInSlip.objects.create(
            slip_number="SLIP-LOCK-001",
            client=slip_client,
            payment_type="mixed",
            banking_date=date(2026, 3, 1),
            created_by=staff_user,
            status="draft",
        )

        foreign_donation = DonationFactory(
            campaign=other_campaign,
            payment_method="cheque",
            qa_status="approved",
        )

        request = RequestFactory().post(
            f"/admin/daily-banking/slip/{slip.id}/add-donations/",
            {"donation_ids": json.dumps([str(foreign_donation.id)])},
        )
        request.user = staff_user

        response = slip_add_donations(request, slip_id=slip.id)

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert "different client" in payload["error"]

    def test_client_supporter_detail_scopes_duplicate_urn_to_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Supporter detail resolves duplicate URNs within the active client only."""
        request_user = UserFactory(is_staff=False, is_superuser=False)
        client_one = ClientFactory(name="Client One")
        client_two = ClientFactory(name="Client Two")

        ClientPortalUser.objects.create(
            client=client_one,
            user=request_user,
            role="viewer",
            is_active=True,
        )

        donor_client_one = DonorFactory(
            client=client_one,
            urn="URN-DUPLICATE-001",
            first_name="Alice",
            last_name="One",
        )
        donor_client_two = DonorFactory(
            client=client_two,
            urn="URN-DUPLICATE-001",
            first_name="Bob",
            last_name="Two",
        )

        campaign_one = CampaignFactory(client=client_one, status=Campaign.STATUS_ACTIVE)
        campaign_two = CampaignFactory(client=client_two, status=Campaign.STATUS_ACTIVE)

        DonationFactory(campaign=campaign_one, donor=donor_client_one)
        DonationFactory(campaign=campaign_two, donor=donor_client_two)

        captured_context: dict[str, Any] = {}

        def fake_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return JsonResponse({"ok": True})

        monkeypatch.setattr("client_portal.views.render", fake_render)

        request = RequestFactory().get("/client/supporters/URN-DUPLICATE-001/")
        request.user = request_user

        response = client_supporter_detail(request, urn="URN-DUPLICATE-001")

        assert response.status_code == 200
        assert isinstance(captured_context["donor"], Donor)
        assert captured_context["donor"].id == donor_client_one.id

    def test_invoice_create_rejects_campaign_from_other_client(self) -> None:
        """Posting a cross-client (client_id, campaign_id) pair 404s at create."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        selected_client = ClientFactory()
        foreign_campaign = CampaignFactory()

        request = RequestFactory().post(
            "/admin/invoices/create/",
            {
                "client_id": str(selected_client.id),
                "campaign_id": str(foreign_campaign.id),
                "billing_period_start": "2026-01-01",
                "billing_period_end": "2026-01-31",
                "due_days": "30",
                "tax_rate": "0",
                "notes": "",
                "terms_and_conditions": "",
                "selected_services": "[]",
            },
        )
        request.user = staff_user

        with pytest.raises(Http404):
            _handle_invoice_create_post(request)

    def test_donor_search_scopes_to_campaign_client(self) -> None:
        """URN collisions across clients are filtered out by campaign scope."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        client_a = ClientFactory(name="Donor Search A")
        client_b = ClientFactory(name="Donor Search B")

        shared_urn = "URN-SEARCH-001"
        DonorFactory(
            client=client_a,
            urn=shared_urn,
            first_name="Alice",
            last_name="A",
        )
        DonorFactory(
            client=client_b,
            urn=shared_urn,
            first_name="Bob",
            last_name="B",
        )
        campaign_a = CampaignFactory(client=client_a, status=Campaign.STATUS_ACTIVE)

        request = RequestFactory().get(
            "/admin/api/donor-search/",
            {"q": shared_urn, "campaign_id": str(campaign_a.id)},
        )
        request.user = staff_user

        response = donor_search(request)

        assert response.status_code == 200
        payload = json.loads(response.content)
        returned_names = {
            f"{entry.get('first_name', '')} {entry.get('last_name', '')}".strip()
            for entry in payload["donors"]
        }
        assert "Alice A" in returned_names
        assert "Bob B" not in returned_names
