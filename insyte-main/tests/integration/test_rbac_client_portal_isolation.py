"""RBAC: anonymous, staff, and cross-client access matrix for ``/client/*``.

Targets the gaps left by ``tests/integration/test_multitenancy_deep.py``,
``tests/unit/test_client_portal_supporter_views.py``, and
``tests/unit/test_client_portal_report_views.py``:

1. CSV export (``/client/reports/export/``) when a different client's
   campaign id is passed in must yield zero data rows.
2. PDF export (``/client/reports/export/pdf/``) with a foreign campaign id
   must return ``403`` (matches the HTMX results contract).
3. Every ``/client/*`` URL redirects an anonymous user away.
4. Every ``/client/*`` URL redirects a staff user back to ``/admin/``.

These tests intentionally walk the full URL list from ``client_portal/urls.py``
so a future route addition that forgets ``@login_required`` or the staff
redirect is caught.
"""

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from campaigns.models import Campaign
from client_portal import middleware as middleware_module
from client_portal.middleware import ClientPortalMiddleware
from client_portal.views import client_report_export, client_report_export_pdf
from clients.models import ClientPortalUser
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)


def _make_portal_user(client: Any) -> Any:
    """Create an active portal user mapped to the given client."""
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


def _client_portal_urls() -> list[tuple[str, str]]:
    """Return ``(name, path)`` for every URL in the ``client_portal`` namespace.

    Uses placeholder values for path parameters so the URL resolves to a
    plausible string. The tests below only care about routing/middleware
    behavior, so the placeholders never need to map to real records.
    """
    placeholder_uuid = str(uuid4())
    return [
        ("dashboard", reverse("client_portal:dashboard")),
        ("reports", reverse("client_portal:reports")),
        ("report_export", reverse("client_portal:report_export")),
        ("report_export_pdf", reverse("client_portal:report_export_pdf")),
        (
            "reports_filter_partial",
            reverse(
                "client_portal:reports_filter_partial",
                args=["donations"],
            ),
        ),
        ("reports_results_partial", reverse("client_portal:reports_results_partial")),
        ("client_supporters", reverse("client_portal:client_supporters")),
        (
            "client_supporter_detail",
            reverse(
                "client_portal:client_supporter_detail",
                args=["URN-NONEXISTENT"],
            ),
        ),
        (
            "client_supporter_detail_data_file",
            reverse(
                "client_portal:client_supporter_detail_data_file",
                args=[placeholder_uuid],
            ),
        ),
        (
            "scan_form_view",
            reverse(
                "client_portal:scan_form_view",
                args=[placeholder_uuid],
            ),
        ),
    ]


@pytest.mark.django_db()
class TestAnonymousCannotReachClientPortal:
    """Every portal URL must reject unauthenticated requests."""

    def test_anonymous_redirects_for_every_client_url(self, client: Any) -> None:
        """Anonymous users 302 away from every ``/client/*`` URL.

        Walks the full URL table so a route added without ``@login_required``
        gets caught immediately.
        """
        for name, path in _client_portal_urls():
            response = client.get(path)
            assert response.status_code in (301, 302), (
                f"Anonymous request to {name} ({path}) returned "
                f"{response.status_code}, expected 301/302"
            )
            location = response.headers.get("Location", "")
            assert "/auth/" in location or "/login" in location, (
                f"Anonymous request to {name} ({path}) redirected to "
                f"{location}, expected an auth URL"
            )


@pytest.mark.django_db()
class TestStaffRedirectedFromClientPortal:
    """Staff users must be bounced from every ``/client/*`` URL by middleware."""

    def test_staff_redirect_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Middleware sends staff users hitting ``/client/*`` to ``/admin/*``.

        Calls ``ClientPortalMiddleware.process_request`` directly with 2FA
        stubbed so the routing branch is tested in isolation.
        """
        staff = UserFactory(is_staff=True)
        monkeypatch.setattr(
            middleware_module,
            "_has_confirmed_2fa_device",
            lambda _u: True,
        )
        monkeypatch.setattr(staff, "is_verified", lambda: True, raising=False)

        rf = RequestFactory()
        middleware = ClientPortalMiddleware(lambda _r: HttpResponse())

        for name, path in _client_portal_urls():
            request = rf.get(path)
            request.user = staff
            response = middleware.process_request(request)
            assert response is not None, (
                f"Staff user on {name} ({path}) was not redirected"
            )
            assert response.status_code == 302
            assert "/admin/" in response.url, (
                f"Staff user on {name} ({path}) redirected to "
                f"{response.url}, expected /admin/"
            )


@pytest.mark.django_db()
class TestReportExportCrossClientIsolation:
    """CSV and PDF exports must never include another client's rows."""

    def test_csv_export_with_foreign_campaign_id_returns_no_data_rows(
        self,
    ) -> None:
        """Passing client B's campaign id yields headers only — no donation rows.

        ``get_portal_export_donations`` filters by ``campaign__client=client``
        before applying the optional ``campaign_id`` filter, so the foreign
        campaign id intersects to an empty queryset.
        """
        client_a = ClientFactory(name="Portal Client A")
        client_b = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_a)

        own_campaign = CampaignFactory(client=client_a, status=Campaign.STATUS_ACTIVE)
        foreign_campaign = CampaignFactory(
            client=client_b, status=Campaign.STATUS_ACTIVE
        )

        own_donor = DonorFactory(client=client_a, urn="URN-A-001")
        foreign_donor = DonorFactory(client=client_b, urn="URN-B-001")
        DonationFactory(campaign=own_campaign, donor=own_donor, amount="11.00")
        DonationFactory(campaign=foreign_campaign, donor=foreign_donor, amount="22.00")

        request = RequestFactory().get(
            reverse("client_portal:report_export"),
            {
                "date_from": "01/01/2026",
                "date_to": "31/12/2026",
                "campaign": str(foreign_campaign.id),
            },
        )
        request.user = portal_user

        response = client_report_export(request)
        body = response.content.decode()

        assert response.status_code == 200
        # Header row is always present; no donation rows must follow.
        non_empty_lines = [line for line in body.splitlines() if line.strip()]
        assert len(non_empty_lines) == 1, (
            "CSV export must contain only the header row when a foreign "
            f"campaign id is supplied; got {non_empty_lines!r}"
        )
        assert "URN-B-001" not in body
        assert "22.00" not in body

    def test_csv_export_without_filter_excludes_other_client_rows(self) -> None:
        """No ``campaign`` filter still scopes rows to the active client only."""
        client_a = ClientFactory(name="Portal Client A")
        client_b = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_a)

        own_campaign = CampaignFactory(client=client_a, status=Campaign.STATUS_ACTIVE)
        foreign_campaign = CampaignFactory(
            client=client_b, status=Campaign.STATUS_ACTIVE
        )

        own_donor = DonorFactory(client=client_a, urn="URN-A-002")
        foreign_donor = DonorFactory(client=client_b, urn="URN-B-002")
        DonationFactory(campaign=own_campaign, donor=own_donor, amount="33.00")
        DonationFactory(campaign=foreign_campaign, donor=foreign_donor, amount="44.00")

        request = RequestFactory().get(
            reverse("client_portal:report_export"),
            {"date_from": "01/01/2026", "date_to": "31/12/2026"},
        )
        request.user = portal_user

        response = client_report_export(request)
        body = response.content.decode()

        assert response.status_code == 200
        assert "URN-A-002" in body
        assert "URN-B-002" not in body
        assert "44.00" not in body

    def test_pdf_export_rejects_foreign_campaign_id(self) -> None:
        """``client_report_export_pdf`` mirrors the HTMX 403 contract.

        ``campaign_belongs_to_client`` returns False both for non-existent
        campaigns and for campaigns owned by a different client; the response
        body is identical, which keeps the endpoint enumeration-safe.
        """
        client_a = ClientFactory(name="Portal Client A")
        client_b = ClientFactory(name="Portal Client B")
        portal_user = _make_portal_user(client_a)

        foreign_campaign = CampaignFactory(
            client=client_b, status=Campaign.STATUS_ACTIVE
        )

        request = RequestFactory().post(
            reverse("client_portal:report_export_pdf"),
            data=(
                '{"report_type":"donations","campaign_id":"'
                + str(foreign_campaign.id)
                + '","date_from":"2026-01-01","date_to":"2026-12-31"}'
            ),
            content_type="application/json",
        )
        request.user = portal_user

        response = client_report_export_pdf(request)

        assert response.status_code == 403
        assert response.content == b"Forbidden"

    def test_pdf_export_rejects_random_campaign_id_with_same_response(
        self,
    ) -> None:
        """A bogus uuid returns the exact same 403 body — no enumeration leak."""
        client_a = ClientFactory(name="Portal Client A")
        portal_user = _make_portal_user(client_a)

        nonexistent_id = str(uuid4())

        request = RequestFactory().post(
            reverse("client_portal:report_export_pdf"),
            data=(
                '{"report_type":"donations","campaign_id":"'
                + nonexistent_id
                + '","date_from":"2026-01-01","date_to":"2026-12-31"}'
            ),
            content_type="application/json",
        )
        request.user = portal_user

        response = client_report_export_pdf(request)

        assert response.status_code == 403
        assert response.content == b"Forbidden"


@pytest.mark.django_db()
class TestPortalUserNeverReceives403WithObjectHints:
    """The path-parameter routes must use 404 (not 403) for foreign objects.

    A ``403`` body that confirms "this URN exists but isn't yours" lets a
    portal user enumerate URNs across the platform. The supporter and
    scan-form views are deliberately built on ``get_object_or_404`` /
    ``Http404`` to avoid that — these tests pin that contract.
    """

    def test_supporter_detail_uses_404_not_403_for_foreign_urn(
        self,
    ) -> None:
        """An owned URN that exists for client B → 404 from client A's session."""
        from client_portal.views import client_supporter_detail

        client_a = ClientFactory()
        client_b = ClientFactory()
        portal_user = _make_portal_user(client_a)

        # Donor exists under client B with active donations.
        foreign_campaign = CampaignFactory(
            client=client_b, status=Campaign.STATUS_ACTIVE
        )
        foreign_donor = DonorFactory(client=client_b, urn="URN-FOREIGN-DETAIL")
        DonationFactory(campaign=foreign_campaign, donor=foreign_donor)

        request = RequestFactory().get(f"/client/supporters/{foreign_donor.urn}/")
        request.user = portal_user

        from django.http import Http404

        with pytest.raises(Http404):
            client_supporter_detail(request, urn=foreign_donor.urn)

    def test_scan_form_view_does_not_invoke_storage_for_foreign_donation(
        self,
    ) -> None:
        """The view must 404 before it ever asks for the placeholder's pages.

        Touching ``DonationScanService.get_placeholder_page_urls`` would imply
        the queryset filter in ``client_scan_form_view`` was bypassed.
        """
        from django.http import Http404

        from client_portal.views import client_scan_form_view

        client_a = ClientFactory()
        client_b = ClientFactory()
        portal_user = _make_portal_user(client_a)

        foreign_campaign = CampaignFactory(
            client=client_b, status=Campaign.STATUS_ACTIVE
        )
        foreign_donor = DonorFactory(client=client_b, urn="URN-FOREIGN-SCAN")
        foreign_donation = DonationFactory(
            campaign=foreign_campaign, donor=foreign_donor
        )

        request = RequestFactory().get(
            f"/client/donations/{foreign_donation.id}/scan-form/"
        )
        request.user = portal_user

        with (
            patch(
                "scans.donation_scan.DonationScanService.get_placeholder_page_urls"
            ) as mock_get_pages,
            pytest.raises(Http404),
        ):
            client_scan_form_view(request, donation_id=str(foreign_donation.id))

        assert not mock_get_pages.called, (
            "Scan-form view leaked: storage URL helper was called for a "
            "donation owned by another client"
        )
