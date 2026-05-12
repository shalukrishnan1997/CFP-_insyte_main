"""Regression tests for client portal report views."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from campaigns.models import Campaign
from client_portal.views import (
    client_dashboard,
    client_report_export,
    client_report_export_pdf,
    client_reports_filter_partial,
    client_reports_results_partial,
)
from clients.models import ClientPortalUser
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)


def _create_portal_user(*, client: Any) -> Any:
    """Create an authenticated user mapped to a client portal profile."""
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


@pytest.mark.django_db()
class TestClientPortalReportViews:
    """Focused regression tests for the refactored client portal report views."""

    def test_results_partial_rejects_campaign_from_other_client(self) -> None:
        """Report results block access to campaigns outside the active client."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        other_client = ClientFactory(name="Other Client")
        portal_user = _create_portal_user(client=client)
        foreign_campaign = CampaignFactory(client=other_client)

        request = request_factory.get(
            "/portal/reports/htmx/results/",
            {
                "report_type": "donations",
                "campaign_id": str(foreign_campaign.id),
            },
        )
        request.user = portal_user

        response = client_reports_results_partial(request)

        assert response.status_code == 403

    def test_results_partial_invalid_per_page_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid per-page values preserve the portal default of 25 rows."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        captured_context: dict[str, Any] = {}

        def fake_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return HttpResponse("ok")

        monkeypatch.setattr("client_portal.views.render", fake_render)
        monkeypatch.setattr(
            "custom_admin.views.reports.main._build_report_context",
            lambda **kwargs: {
                "report_title": "Donations",
                "report_description": "Donation report",
                "table_headers": [],
                "report_data": [],
                "total_records": 0,
                "page_start": 0,
                "page_end": 0,
                "current_page": 1,
                "has_previous": False,
                "has_next": False,
                "previous_page": None,
                "next_page": None,
                "per_page": kwargs["per_page"],
                "pagination_query": "",
                "chart_data_json": "null",
                "has_visualization": False,
                "pdf_row_warning": False,
            },
        )

        request = request_factory.get(
            "/portal/reports/htmx/results/",
            {
                "report_type": "donations",
                "per_page": "not-a-number",
            },
        )
        request.user = portal_user

        response = client_reports_results_partial(request)

        assert response.status_code == 200
        assert captured_context["per_page"] == 25
        assert captured_context["client_id"] == str(client.id)
        assert captured_context["scope"] == "client"

    def test_filter_partial_scopes_campaign_choices_to_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Filter panels only expose campaigns owned by the active portal client."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        other_client = ClientFactory(name="Other Client")
        visible_campaign = CampaignFactory(client=client, name="Visible Campaign")
        CampaignFactory(client=other_client, name="Hidden Campaign")
        portal_user = _create_portal_user(client=client)
        captured_context: dict[str, Any] = {}

        def fake_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return HttpResponse("ok")

        monkeypatch.setattr("client_portal.views.render", fake_render)

        request = request_factory.get(
            "/portal/reports/htmx/filters/donations/",
            {
                "campaign_id": str(visible_campaign.id),
                "per_page": "9999",
            },
        )
        request.user = portal_user

        response = client_reports_filter_partial(request, report_type="donations")

        assert response.status_code == 200
        assert captured_context["per_page"] == 500
        assert [campaign.id for campaign in captured_context["all_campaigns"]] == [
            visible_campaign.id
        ]

    def test_dashboard_scopes_metrics_to_active_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dashboard metrics aggregate only the active client's campaigns."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        other_client = ClientFactory(name="Other Client")
        portal_user = _create_portal_user(client=client)
        active_campaign = CampaignFactory(
            client=client,
            name="Visible Active",
            status=Campaign.STATUS_ACTIVE,
        )
        CampaignFactory(
            client=client,
            name="Visible Draft",
            status=Campaign.STATUS_DRAFT,
        )
        foreign_campaign = CampaignFactory(
            client=other_client,
            name="Hidden Foreign",
            status=Campaign.STATUS_ACTIVE,
        )
        donor = DonorFactory(client=client, first_name="Alice", last_name="Smith")
        foreign_donor = DonorFactory(
            client=other_client,
            first_name="Bob",
            last_name="Jones",
        )
        DonationFactory(
            campaign=active_campaign,
            donor=donor,
            amount=Decimal("10.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=active_campaign,
            donor=donor,
            amount=Decimal("5.00"),
            donation_date=date(2026, 3, 21),
        )
        DonationFactory(
            campaign=foreign_campaign,
            donor=foreign_donor,
            amount=Decimal("99.00"),
            donation_date=date(2026, 3, 20),
        )

        captured_context: dict[str, Any] = {}

        def fake_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return HttpResponse("ok")

        monkeypatch.setattr("client_portal.views.render", fake_render)

        request = request_factory.get("/portal/")
        request.user = portal_user

        response = client_dashboard(request)

        assert response.status_code == 200
        assert captured_context["total_campaigns"] == 2
        assert captured_context["active_campaigns"] == 1
        assert captured_context["total_donations"] == 2
        assert captured_context["total_amount"] == Decimal("15.00")
        assert captured_context["avg_donation"] == Decimal("7.50")
        assert all(
            campaign_row["campaign__name"] != foreign_campaign.name
            for campaign_row in captured_context["top_campaigns"]
        )
        assert captured_context["active"] == "dashboard"

    def test_report_export_accepts_uk_dates_and_campaign_filter(self) -> None:
        """CSV export keeps UK date parsing and campaign-scoped filenames."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(client=client, name="Spring Appeal")
        donor = DonorFactory(
            client=client,
            first_name="Alice",
            last_name="Smith",
            urn="URN-ALICE-001",
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("10.50"),
            payment_method="card",
            gift_aid=True,
            donation_date=date(2026, 3, 20),
        )

        request = request_factory.get(
            "/portal/reports/export/",
            {
                "date_from": "20/03/2026",
                "date_to": "20/03/2026",
                "campaign": str(campaign.id),
            },
        )
        request.user = portal_user

        response = client_report_export(request)
        content = response.content.decode()

        assert response.status_code == 200
        assert "Spring_Appeal" in response["Content-Disposition"]
        assert "20/03/2026" in content
        assert "Alice Smith" in content
        assert "10.50" in content

    def test_report_export_sanitizes_formula_like_cells(self) -> None:
        """CSV export guards against spreadsheet formula injection in donor fields."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(client=client, name="Spring Appeal")
        donor = DonorFactory(
            client=client,
            first_name="=SUM(1,1)",
            last_name="Smith",
            urn="URN-ALICE-001",
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("10.50"),
            payment_method="card",
            gift_aid=True,
            donation_date=date(2026, 3, 20),
        )

        request = request_factory.get(
            "/portal/reports/export/",
            {
                "date_from": "20/03/2026",
                "date_to": "20/03/2026",
                "campaign": str(campaign.id),
            },
        )
        request.user = portal_user

        response = client_report_export(request)
        content = response.content.decode()

        assert response.status_code == 200
        assert "'=SUM(1,1) Smith" in content

    def test_report_export_unmatched_donors_sanitizes_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unmatched donor CSV exports keep the same branch and sanitize cell content."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)

        monkeypatch.setattr(
            "custom_admin.views.reports.generators._generate_unmatched_donors_report",
            lambda **_kwargs: [["=danger", "@postcode"]],
        )

        request = request_factory.get(
            "/portal/reports/export/",
            {
                "report_type": "unmatched_donors",
                "date_from": "20/03/2026",
                "date_to": "20/03/2026",
            },
        )
        request.user = portal_user

        response = client_report_export(request)
        content = response.content.decode()

        assert response.status_code == 200
        assert "'=danger" in content
        assert "'@postcode" in content

    def test_report_export_pdf_returns_pdf_for_owned_campaign(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PDF export returns an attachment for valid client-scoped requests."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(client=client, name="Spring Appeal")

        monkeypatch.setattr(
            "custom_admin.views.reports.helpers._get_filtered_donations",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            "custom_admin.views.reports.main._run_report_generator",
            lambda *_args, **_kwargs: [["Campaign", "10.00"]],
        )

        request = request_factory.post(
            "/portal/reports/export/pdf/",
            data=(
                '{"report_type":"donations","campaign_id":"'
                + str(campaign.id)
                + '","date_from":"2026-03-20","date_to":"2026-03-20"}'
            ),
            content_type="application/json",
        )
        request.user = portal_user

        response = client_report_export_pdf(request)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].endswith('2026-03-20_to_2026-03-20.pdf"')
        assert response.content.startswith(b"%PDF")

    def test_report_export_pdf_rejects_invalid_json_body(self) -> None:
        """PDF export keeps the JSON validation contract for malformed bodies."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)

        request = request_factory.post(
            "/portal/reports/export/pdf/",
            data="{not-json}",
            content_type="application/json",
        )
        request.user = portal_user

        response = client_report_export_pdf(request)

        assert response.status_code == 400
        assert json.loads(response.content) == {"error": "Invalid JSON body."}


@pytest.mark.django_db()
class TestPhoneDonationsInPortalReports:
    """Phone-intake donations must surface in client-portal views.

    The portal applies its own client-scope filter on top of the shared
    ``_get_filtered_donations``. These tests pin that phone-marked
    donations remain visible in both the dashboard totals and the CSV
    export endpoint.
    """

    def test_phone_donation_visible_in_portal_dashboard(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(
            client=client,
            name="Phone Appeal",
            status=Campaign.STATUS_ACTIVE,
        )
        donor = DonorFactory(client=client)
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("60.00"),
            payment_method="card",
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        captured_context: dict[str, Any] = {}

        def fake_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return HttpResponse("ok")

        monkeypatch.setattr("client_portal.views.render", fake_render)

        request = request_factory.get("/portal/")
        request.user = portal_user

        response = client_dashboard(request)

        assert response.status_code == 200
        assert captured_context["total_donations"] == 1
        assert captured_context["total_amount"] == Decimal("60.00")

    def test_phone_donation_in_portal_csv_export(self) -> None:
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(client=client, name="Phone Appeal")
        donor = DonorFactory(
            client=client,
            first_name="Phoned",
            last_name="Donor",
            urn="URN-PHONE-001",
        )
        DonationFactory(
            campaign=campaign,
            donor=donor,
            amount=Decimal("42.00"),
            payment_method="card",
            donation_date=date(2026, 3, 20),
            field_data={"intake_method": "phone"},
        )

        request = request_factory.get(
            "/portal/reports/export/",
            {
                "date_from": "20/03/2026",
                "date_to": "20/03/2026",
                "campaign": str(campaign.id),
            },
        )
        request.user = portal_user

        response = client_report_export(request)
        content = response.content.decode()

        assert response.status_code == 200
        assert "Phoned Donor" in content
        assert "42.00" in content
