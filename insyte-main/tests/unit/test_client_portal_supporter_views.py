"""Regression tests for client portal supporter views."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.http import Http404, HttpRequest, HttpResponse
from django.test import RequestFactory

from campaigns.models import CampaignDataFile
from client_portal.views import (
    client_supporter_detail_data_file,
    client_supporters,
)
from clients.models import ClientPortalUser
from donors.models import DataFileDonor
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


def _create_data_file_donor(*, campaign: Any, client: Any, created_by: Any) -> Any:
    """Create a minimal data-file donor for supporter view tests."""
    data_file = CampaignDataFile.objects.create(
        campaign=campaign,
        created_by=created_by,
    )
    return DataFileDonor.objects.create(
        data_file=data_file,
        client=client,
        urn=f"DF-{campaign.id}",
        first_name="Zara",
        last_name="Data",
        email="zara@example.com",
        postcode="N1 1AA",
        created_by=created_by,
    )


@pytest.mark.django_db()
class TestClientPortalSupporterViews:
    """Focused regression tests for supporter list/detail behavior."""

    def test_client_supporters_merges_house_and_data_file_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Supporter list combines both donor sources for the active client only."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        other_client = ClientFactory(name="Other Client")
        portal_user = _create_portal_user(client=client)

        visible_campaign = CampaignFactory(client=client, name="Visible Campaign")
        CampaignFactory(client=other_client, name="Hidden Campaign")

        house_donor = DonorFactory(
            client=client,
            title="",
            first_name="Alice",
            last_name="House",
            urn="URN-HOUSE-001",
        )
        data_file_donor = _create_data_file_donor(
            campaign=visible_campaign,
            client=client,
            created_by=portal_user,
        )
        hidden_donor = DonorFactory(
            client=other_client,
            title="",
            first_name="Hidden",
            last_name="Donor",
            urn="URN-HIDDEN-001",
        )

        DonationFactory(
            campaign=visible_campaign,
            donor=house_donor,
            amount=Decimal("15.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=visible_campaign,
            donor=None,
            data_file_donor=data_file_donor,
            donor_source="data_file",
            amount=Decimal("22.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=CampaignFactory(client=other_client),
            donor=hidden_donor,
            amount=Decimal("99.00"),
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

        request = request_factory.get(
            "/portal/supporters/",
            {"campaign": str(visible_campaign.id)},
        )
        request.user = portal_user

        response = client_supporters(request)
        donors = list(captured_context["donors"].object_list)

        assert response.status_code == 200
        assert captured_context["total_count"] == 2
        assert {donor.full_name for donor in donors} == {"Alice House", "Zara Data"}
        assert all("Hidden" not in donor.full_name for donor in donors)

    def test_client_supporter_detail_data_file_scopes_pk_to_client(self) -> None:
        """Data-file supporter detail rejects supporters outside the active client."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        other_client = ClientFactory(name="Other Client")
        portal_user = _create_portal_user(client=client)

        foreign_campaign = CampaignFactory(client=other_client)
        foreign_donor = _create_data_file_donor(
            campaign=foreign_campaign,
            client=other_client,
            created_by=portal_user,
        )
        DonationFactory(
            campaign=foreign_campaign,
            donor=None,
            data_file_donor=foreign_donor,
            donor_source="data_file",
        )

        request = request_factory.get(
            f"/portal/supporters/data-file/{foreign_donor.id}/"
        )
        request.user = portal_user

        with pytest.raises(Http404):
            client_supporter_detail_data_file(request, pk=str(foreign_donor.id))

    def test_client_supporter_detail_data_file_builds_scoped_stats(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Data-file supporter detail aggregates only the active client's donations."""
        request_factory = RequestFactory()
        client = ClientFactory(name="Portal Client")
        portal_user = _create_portal_user(client=client)
        campaign = CampaignFactory(client=client)
        donor = _create_data_file_donor(
            campaign=campaign,
            client=client,
            created_by=portal_user,
        )

        DonationFactory(
            campaign=campaign,
            donor=None,
            data_file_donor=donor,
            donor_source="data_file",
            amount=Decimal("10.00"),
            donation_date=date(2026, 3, 20),
        )
        DonationFactory(
            campaign=campaign,
            donor=None,
            data_file_donor=donor,
            donor_source="data_file",
            amount=Decimal("25.00"),
            donation_date=date(2026, 3, 19),
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

        request = request_factory.get(f"/portal/supporters/data-file/{donor.id}/")
        request.user = portal_user

        response = client_supporter_detail_data_file(request, pk=str(donor.id))

        assert response.status_code == 200
        assert captured_context["donor"] == donor
        assert captured_context["donation_count"] == 2
        assert captured_context["total_donated"] == Decimal("35.00")
