"""Client portal isolation: cross-client access must be unreachable.

Each test asserts that a portal user authenticated against ``client_one``
cannot see, count, or 404-probe data belonging to ``client_two``.

Portal views are called directly via ``RequestFactory`` to bypass middleware
and 2FA; middleware-routing tests invoke ``ClientPortalMiddleware.process_request``
directly and stub the 2FA gate so the routing branch is exercised in isolation.
"""

from typing import Any
from uuid import uuid4

import pytest
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.test import RequestFactory

from campaigns.models import Campaign
from client_portal import middleware as middleware_module
from client_portal.middleware import ClientPortalMiddleware
from client_portal.views import (
    client_dashboard,
    client_scan_form_view,
    client_supporter_detail,
    client_supporters,
)
from clients.models import ClientPortalUser
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


def _make_portal_user(client: Any, *, is_active: bool = True) -> Any:
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=is_active,
    )
    return user


def _seed_client_with_one_donation(client: Any) -> tuple[Any, Any, Any]:
    campaign = CampaignFactory(client=client, status=Campaign.STATUS_ACTIVE)
    donor = DonorFactory(
        client=client,
        urn=f"URN-{uuid4().hex[:8]}",
    )
    donation = DonationFactory(campaign=campaign, donor=donor, amount="42.00")
    return campaign, donor, donation


def _capture_render(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``client_portal.views.render`` with a context-capturing stub."""
    captured: dict[str, Any] = {}

    def fake_render(
        _request: HttpRequest,
        _template_name: str,
        context: dict[str, Any],
    ) -> HttpResponse:
        captured.update(context)
        return JsonResponse({"ok": True})

    monkeypatch.setattr("client_portal.views.render", fake_render)
    return captured


@pytest.mark.django_db()
class TestPortalUserCannotReadOtherClientData:
    """Direct-URL probes into another client's data must be invisible."""

    def test_supporters_list_excludes_other_clients_donors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory(name="Portal Client A")
        client_two = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_one)

        _, donor_a, _ = _seed_client_with_one_donation(client_one)
        _, donor_b, _ = _seed_client_with_one_donation(client_two)

        captured = _capture_render(monkeypatch)
        request = RequestFactory().get("/client/supporters/")
        request.user = portal_user

        response = client_supporters(request)

        assert response.status_code == 200
        urns = {row.urn for row in captured["donors"].object_list}
        assert donor_a.urn in urns
        assert donor_b.urn not in urns

    def test_supporter_detail_404s_for_other_clients_urn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory(name="Portal Client A")
        client_two = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_one)

        _seed_client_with_one_donation(client_one)
        _, donor_b, _ = _seed_client_with_one_donation(client_two)

        _capture_render(monkeypatch)
        request = RequestFactory().get(f"/client/supporters/{donor_b.urn}/")
        request.user = portal_user

        with pytest.raises(Http404):
            client_supporter_detail(request, urn=donor_b.urn)

    def test_scan_form_view_404s_for_other_clients_donation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory(name="Portal Client A")
        client_two = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_one)

        _seed_client_with_one_donation(client_one)
        campaign_b, donor_b, _ = _seed_client_with_one_donation(client_two)
        scan_batch = ScanBatchFactory(campaign=campaign_b)
        foreign_donation = DonationFactory(campaign=campaign_b, donor=donor_b)
        ScanPlaceholderFactory(
            batch=scan_batch,
            urn=donor_b.urn,
            donation=foreign_donation,
        )

        _capture_render(monkeypatch)
        request = RequestFactory().get(
            f"/client/donations/{foreign_donation.id}/scan-form/"
        )
        request.user = portal_user

        with pytest.raises(Http404):
            client_scan_form_view(request, donation_id=str(foreign_donation.id))


@pytest.mark.django_db()
class TestPortalDashboardIsolation:
    """Dashboard totals only include the authenticated client's data."""

    def test_dashboard_numbers_exclude_other_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django.core.cache import cache

        cache.clear()

        client_one = ClientFactory(name="Portal Client A")
        client_two = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_one)

        campaign_a = CampaignFactory(client=client_one, status=Campaign.STATUS_ACTIVE)
        campaign_b = CampaignFactory(client=client_two, status=Campaign.STATUS_ACTIVE)

        DonationFactory(campaign=campaign_a, amount="50.00")
        DonationFactory(campaign=campaign_a, amount="75.00")
        DonationFactory(campaign=campaign_b, amount="10000.00")
        DonationFactory(campaign=campaign_b, amount="99999.00")

        captured = _capture_render(monkeypatch)
        request = RequestFactory().get("/client/")
        request.user = portal_user

        response = client_dashboard(request)

        assert response.status_code == 200
        assert captured["total_donations"] == 2
        assert float(captured["total_amount"]) == 125.00
        assert captured["total_campaigns"] == 1


@pytest.mark.django_db()
class TestPortalAccessGates:
    """Portal ``client_required`` decorator blocks inactive profiles and clients."""

    def test_inactive_portal_user_is_redirected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory()
        portal_user = _make_portal_user(client_one, is_active=False)

        _capture_render(monkeypatch)
        request = RequestFactory().get("/client/")
        request.user = portal_user

        response = client_dashboard(request)

        assert response.status_code == 302

    def test_inactive_client_is_redirected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory(is_active=False)
        portal_user = _make_portal_user(client_one, is_active=True)

        _capture_render(monkeypatch)
        request = RequestFactory().get("/client/")
        request.user = portal_user

        response = client_dashboard(request)

        assert response.status_code == 302


@pytest.mark.django_db()
class TestPortalMiddlewareRouting:
    """``ClientPortalMiddleware`` cross-routes users who hit the wrong prefix."""

    @staticmethod
    def _bypass_2fa(monkeypatch: pytest.MonkeyPatch, user: Any) -> None:
        monkeypatch.setattr(
            middleware_module,
            "_has_confirmed_2fa_device",
            lambda _u: True,
        )
        monkeypatch.setattr(user, "is_verified", lambda: True, raising=False)

    def test_staff_on_client_path_redirects_to_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff = UserFactory(is_staff=True)
        self._bypass_2fa(monkeypatch, staff)

        request = RequestFactory().get("/client/supporters/")
        request.user = staff

        response = ClientPortalMiddleware(lambda _r: HttpResponse()).process_request(
            request
        )

        assert response is not None
        assert response.status_code == 302
        assert "/admin/" in response.url

    def test_portal_user_on_admin_path_redirects_to_portal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_one = ClientFactory()
        portal_user = _make_portal_user(client_one)
        self._bypass_2fa(monkeypatch, portal_user)

        request = RequestFactory().get("/admin/invoices/")
        request.user = portal_user

        response = ClientPortalMiddleware(lambda _r: HttpResponse()).process_request(
            request
        )

        assert response is not None
        assert response.status_code == 302
        assert "/client/" in response.url
