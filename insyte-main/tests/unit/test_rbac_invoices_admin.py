"""RBAC tests for invoices admin endpoints.

Covers anonymous denial, client portal user denial via ClientPortalMiddleware,
staff allowance, and cross-client (IDOR) protections for every invoice and
service-settings URL under ``/admin/``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.http import Http404, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from client_portal.middleware import ClientPortalMiddleware
from clients.models import ClientPortalUser
from invoices.admin_views import _handle_invoice_create_post
from invoices.models import Invoice, ServiceCategory, ServiceItem
from tests.factories import CampaignFactory, ClientFactory, UserFactory


def _create_invoice(*, client: Any = None, campaign: Any = None) -> Invoice:
    """Create a minimal persisted invoice for RBAC tests."""
    user = UserFactory(is_staff=True)
    if campaign is None:
        campaign = CampaignFactory(client=client) if client else CampaignFactory()
    return Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        billing_period_start=date.today() - timedelta(days=30),
        billing_period_end=date.today(),
        due_date=date.today() + timedelta(days=30),
        created_by=user,
        status=Invoice.STATUS_DRAFT,
        service_fee=Decimal("100.00"),
        tax_rate=Decimal("0.00"),
    )


def _create_portal_user(*, client: Any) -> Any:
    """Create a non-staff user with a ClientPortalUser profile."""
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


def _run_portal_middleware(rf: RequestFactory, user: Any, path: str) -> Any:
    """Invoke ClientPortalMiddleware against a path with 2FA bypassed via patch.

    Tests file disables the middleware globally; we instantiate it directly
    so we can assert the routing decision (admin->client redirect for
    portal users; login redirect for anonymous).
    """

    def _get_response(_request: Any) -> HttpResponse:
        return HttpResponse("ok")

    middleware = ClientPortalMiddleware(_get_response)
    request = rf.get(path)
    request.user = user
    user.is_verified = lambda: True  # type: ignore[method-assign]

    with (
        patch("client_portal.middleware.resolve") as mock_resolve,
        patch("client_portal.middleware._has_confirmed_2fa_device", return_value=True),
    ):
        mock_url = MagicMock()
        mock_url.url_name = "invoice_list"
        mock_resolve.return_value = mock_url
        return middleware.process_request(request)


@pytest.mark.django_db()
class TestInvoicesAdminAnonymous:
    """Anonymous users must be redirected to login on every URL."""

    def _expect_login_redirect(self, response: Any) -> None:
        assert response.status_code == 302
        # is_authenticated_and_is_staff redirects to LOGIN_URL.
        assert "/auth/login/" in response.url

    def test_invoice_list_anonymous_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:invoice_list"))
        self._expect_login_redirect(response)

    def test_invoice_create_anonymous_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:invoice_create"))
        self._expect_login_redirect(response)

    def test_invoice_metrics_api_anonymous_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:invoice_metrics_api"))
        self._expect_login_redirect(response)

    def test_invoice_detail_anonymous_redirects(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.get(reverse("custom_admin:invoice_detail", args=[invoice.id]))
        self._expect_login_redirect(response)

    def test_invoice_mark_paid_anonymous_redirects(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_mark_paid", args=[invoice.id]),
            {"amount": "100.00", "payment_method": "credit_card"},
        )
        self._expect_login_redirect(response)
        invoice.refresh_from_db()
        assert invoice.amount_paid == Decimal("0.00")
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_change_status_anonymous_redirects(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_change_status", args=[invoice.id]),
            {"status": Invoice.STATUS_CANCELLED},
        )
        self._expect_login_redirect(response)
        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_pdf_anonymous_no_pdf(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.get(
            reverse("custom_admin:invoice_generate_pdf", args=[invoice.id])
        )
        self._expect_login_redirect(response)
        assert response["Content-Type"] != "application/pdf"

    def test_invoice_preview_pdf_anonymous_no_pdf(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.get(
            reverse("custom_admin:invoice_preview_pdf", args=[invoice.id])
        )
        self._expect_login_redirect(response)
        assert response["Content-Type"] != "application/pdf"

    def test_invoice_edit_anonymous_redirects(self, client: Client) -> None:
        invoice = _create_invoice()
        response = client.get(reverse("custom_admin:invoice_edit", args=[invoice.id]))
        self._expect_login_redirect(response)

    def test_invoice_update_line_items_anonymous_redirects(
        self, client: Client
    ) -> None:
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_update_line_items", args=[invoice.id]),
            data=json.dumps({"line_items": []}),
            content_type="application/json",
        )
        self._expect_login_redirect(response)

    def test_service_settings_anonymous_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:admin_service_settings"))
        self._expect_login_redirect(response)

    def test_service_item_create_anonymous_redirects(self, client: Client) -> None:
        response = client.post(reverse("custom_admin:service_item_create"), {})
        self._expect_login_redirect(response)
        assert ServiceItem.objects.count() == 0

    def test_service_item_update_anonymous_redirects(self, client: Client) -> None:
        response = client.post(reverse("custom_admin:service_item_update"), {})
        self._expect_login_redirect(response)

    def test_service_category_create_anonymous_redirects(self, client: Client) -> None:
        response = client.post(reverse("custom_admin:service_category_create"), {})
        self._expect_login_redirect(response)
        assert ServiceCategory.objects.count() == 0

    def test_service_category_update_anonymous_redirects(self, client: Client) -> None:
        response = client.post(reverse("custom_admin:service_category_update"), {})
        self._expect_login_redirect(response)

    def test_service_item_delete_anonymous_redirects(self, client: Client) -> None:
        category = ServiceCategory.objects.create(name="cat-1", order=1)
        item = ServiceItem.objects.create(
            category=category,
            description="svc",
            unit_price=Decimal("10.00"),
            pricing_unit="each",
        )
        response = client.get(
            reverse("custom_admin:service_item_delete", args=[item.id])
        )
        self._expect_login_redirect(response)
        item.refresh_from_db()
        assert item.is_active is True


@pytest.mark.django_db()
class TestInvoicesAdminClientPortalUser:
    """Client portal users hitting /admin/* must be redirected by middleware."""

    def test_portal_user_blocked_on_invoice_list(self, rf: RequestFactory) -> None:
        portal_user = _create_portal_user(client=ClientFactory())
        result = _run_portal_middleware(
            rf, portal_user, reverse("custom_admin:invoice_list")
        )
        assert result is not None
        assert result.status_code == 302
        assert "/client/" in result.url

    def test_portal_user_blocked_on_invoice_detail(self, rf: RequestFactory) -> None:
        portal_user = _create_portal_user(client=ClientFactory())
        invoice = _create_invoice()
        result = _run_portal_middleware(
            rf,
            portal_user,
            reverse("custom_admin:invoice_detail", args=[invoice.id]),
        )
        assert result is not None
        assert result.status_code == 302
        assert "/client/" in result.url

    def test_portal_user_blocked_on_service_settings(self, rf: RequestFactory) -> None:
        portal_user = _create_portal_user(client=ClientFactory())
        result = _run_portal_middleware(
            rf, portal_user, reverse("custom_admin:admin_service_settings")
        )
        assert result is not None
        assert result.status_code == 302
        assert "/client/" in result.url


@pytest.mark.django_db()
class TestInvoicesAdminStaff:
    """Staff users get through to every endpoint (200/302/400, never 403)."""

    def _login(self, client: Client) -> Any:
        user = UserFactory(is_staff=True)
        client.force_login(user)
        return user

    def test_staff_invoice_list_ok(self, client: Client) -> None:
        self._login(client)
        response = client.get(reverse("custom_admin:invoice_list"))
        assert response.status_code == 200

    def test_staff_invoice_create_get_ok(self, client: Client) -> None:
        self._login(client)
        # Need at least one active client so the view doesn't redirect.
        ClientFactory()
        response = client.get(reverse("custom_admin:invoice_create"))
        assert response.status_code == 200

    def test_staff_invoice_metrics_api_validates_params(self, client: Client) -> None:
        self._login(client)
        response = client.get(reverse("custom_admin:invoice_metrics_api"))
        assert response.status_code == 400

    def test_staff_invoice_detail_ok(self, client: Client) -> None:
        self._login(client)
        invoice = _create_invoice()
        response = client.get(reverse("custom_admin:invoice_detail", args=[invoice.id]))
        assert response.status_code == 200

    def test_staff_invoice_mark_paid_updates_invoice(self, client: Client) -> None:
        self._login(client)
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_mark_paid", args=[invoice.id]),
            {
                "amount": "50.00",
                "payment_method": "credit_card",
                "payment_reference": "TXN-1",
                "payment_date": date.today().isoformat(),
            },
        )
        assert response.status_code == 302
        invoice.refresh_from_db()
        assert invoice.amount_paid == Decimal("50.00")

    def test_staff_invoice_change_status_updates_invoice(self, client: Client) -> None:
        self._login(client)
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_change_status", args=[invoice.id]),
            {"status": Invoice.STATUS_CANCELLED},
        )
        assert response.status_code == 302
        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_CANCELLED

    def test_staff_invoice_edit_ok(self, client: Client) -> None:
        self._login(client)
        invoice = _create_invoice()
        response = client.get(reverse("custom_admin:invoice_edit", args=[invoice.id]))
        assert response.status_code == 200

    def test_staff_invoice_update_line_items_ok(self, client: Client) -> None:
        self._login(client)
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_update_line_items", args=[invoice.id]),
            data=json.dumps({"line_items": []}),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_staff_service_settings_ok(self, client: Client) -> None:
        self._login(client)
        response = client.get(reverse("custom_admin:admin_service_settings"))
        assert response.status_code == 200

    def test_staff_service_category_create_ok(self, client: Client) -> None:
        self._login(client)
        response = client.post(
            reverse("custom_admin:service_category_create"),
            data=json.dumps({"name": "Test Category", "description": "Desc"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert ServiceCategory.objects.filter(name="Test Category").exists()

    def test_staff_service_item_create_ok(self, client: Client) -> None:
        self._login(client)
        category = ServiceCategory.objects.create(name="C-1", order=1)
        response = client.post(
            reverse("custom_admin:service_item_create"),
            data=json.dumps(
                {
                    "category_id": str(category.id),
                    "description": "Service A",
                    "unit_price": "12.50",
                    "pricing_unit": "each",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert ServiceItem.objects.filter(description="Service A").exists()

    def test_staff_service_item_delete_soft_deletes(self, client: Client) -> None:
        self._login(client)
        category = ServiceCategory.objects.create(name="C-2", order=1)
        item = ServiceItem.objects.create(
            category=category,
            description="svc",
            unit_price=Decimal("10.00"),
            pricing_unit="each",
        )
        response = client.get(
            reverse("custom_admin:service_item_delete", args=[item.id])
        )
        assert response.status_code == 302
        item.refresh_from_db()
        assert item.is_active is False


@pytest.mark.django_db()
class TestInvoiceCrossClientIDOR:
    """Cross-client URL manipulation must not let a request mutate or read data."""

    def test_invoice_create_rejects_cross_client_campaign(self) -> None:
        """A campaign whose client != selected client must 404 at create."""
        staff_user = UserFactory(is_staff=True)
        selected_client = ClientFactory()
        foreign_campaign = CampaignFactory()
        assert foreign_campaign.client_id != selected_client.id

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

    def test_invoice_metrics_rejects_cross_client_campaign(
        self, client: Client
    ) -> None:
        """Metrics API rejects (client_id, campaign_id) pairs from different tenants."""
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        selected_client = ClientFactory()
        foreign_campaign = CampaignFactory()
        assert foreign_campaign.client_id != selected_client.id

        response = client.get(
            reverse("custom_admin:invoice_metrics_api"),
            {
                "client_id": str(selected_client.id),
                "campaign_id": str(foreign_campaign.id),
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )

        assert response.status_code == 400
        payload = json.loads(response.content)
        assert "does not belong" in payload["error"]

    def test_invoice_detail_renders_for_any_client_invoice(
        self, client: Client
    ) -> None:
        """Invoice detail is not scoped per-tenant (staff are global by design).

        This documents current behavior: a staff session can view any tenant's
        invoice. Cross-client IDOR tests above target client/campaign pairing,
        which is the per-request mutation surface where the bug would matter.
        """
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        client_a = ClientFactory()
        client_b = ClientFactory()
        invoice_a = _create_invoice(client=client_a)
        invoice_b = _create_invoice(client=client_b)

        response_a = client.get(
            reverse("custom_admin:invoice_detail", args=[invoice_a.id])
        )
        response_b = client.get(
            reverse("custom_admin:invoice_detail", args=[invoice_b.id])
        )
        assert response_a.status_code == 200
        assert response_b.status_code == 200

    def test_invoice_mark_paid_404s_on_unknown_invoice(self, client: Client) -> None:
        """A POST against a non-existent UUID 404s rather than silently no-oping."""
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        bogus_id = "00000000-0000-0000-0000-000000000000"
        response = client.post(
            reverse("custom_admin:invoice_mark_paid", args=[bogus_id]),
            {"amount": "10.00"},
        )
        assert response.status_code == 404

    def test_invoice_change_status_404s_on_unknown_invoice(
        self, client: Client
    ) -> None:
        """A POST with an unknown UUID 404s instead of silently no-oping."""
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        bogus_id = "00000000-0000-0000-0000-000000000000"
        response = client.post(
            reverse("custom_admin:invoice_change_status", args=[bogus_id]),
            {"status": Invoice.STATUS_CANCELLED},
        )
        assert response.status_code == 404


@pytest.mark.django_db()
class TestInvoiceStatePOSTOnlyEnforcement:
    """State-changing endpoints must only mutate on POST."""

    def test_invoice_mark_paid_get_does_not_mutate(self, client: Client) -> None:
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        invoice = _create_invoice()
        response = client.get(
            reverse("custom_admin:invoice_mark_paid", args=[invoice.id])
        )
        # GET redirects to detail and does not mutate.
        assert response.status_code == 302
        invoice.refresh_from_db()
        assert invoice.amount_paid == Decimal("0.00")
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_change_status_get_does_not_mutate(self, client: Client) -> None:
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        invoice = _create_invoice()
        response = client.get(
            reverse("custom_admin:invoice_change_status", args=[invoice.id])
        )
        assert response.status_code == 302
        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_change_status_rejects_invalid_status(self, client: Client) -> None:
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_change_status", args=[invoice.id]),
            {"status": "not-a-real-status"},
        )
        # Redirects with error message, status unchanged.
        assert response.status_code == 302
        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_change_status_rejects_paid_via_status_endpoint(
        self, client: Client
    ) -> None:
        """``paid`` is intentionally not in the valid status list — it must use mark-paid."""
        staff = UserFactory(is_staff=True)
        client.force_login(staff)
        invoice = _create_invoice()
        response = client.post(
            reverse("custom_admin:invoice_change_status", args=[invoice.id]),
            {"status": Invoice.STATUS_PAID},
        )
        assert response.status_code == 302
        invoice.refresh_from_db()
        # Status remains draft because PAID is not in the valid list.
        assert invoice.status == Invoice.STATUS_DRAFT
