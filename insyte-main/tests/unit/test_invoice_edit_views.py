"""Unit tests for invoice_edit GET view and invoice_update_line_items AJAX endpoint."""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import RequestFactory

from invoices.admin_views_edit import invoice_edit, invoice_update_line_items
from invoices.models import Invoice
from tests.factories import CampaignFactory, UserFactory


def _create_invoice(
    *,
    service_fee: Decimal = Decimal("100.00"),
    tax_rate: Decimal = Decimal("20.00"),
    status: str = Invoice.STATUS_DRAFT,
) -> Invoice:
    """Create a minimal invoice for invoice edit view tests.

    Args:
        service_fee: Invoice service fee amount.
        tax_rate: VAT/tax rate percentage.
        status: Invoice status string.

    Returns:
        Persisted Invoice instance.
    """
    user = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory(created_by=user)
    return Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        billing_period_start=date.today() - timedelta(days=30),
        billing_period_end=date.today(),
        due_date=date.today() + timedelta(days=30),
        created_by=user,
        status=status,
        service_fee=service_fee,
        tax_rate=tax_rate,
    )


@pytest.mark.django_db()
class TestInvoiceEditView:
    """Tests for the invoice_edit GET view."""

    def setup_method(self) -> None:
        """Set up RequestFactory shared across tests."""
        self.factory = RequestFactory()

    def test_renders_ok_with_metric_based_line_items(self) -> None:
        """GET returns 200 and renders edit page when no session data is present."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        request = self.factory.get(f"/admin/invoices/{invoice.id}/edit/")
        request.user = user
        request.session = {}

        response = invoice_edit(request, invoice_id=str(invoice.id))

        assert response.status_code == 200

    def test_uses_session_selected_services_when_present(self) -> None:
        """GET uses session-stored services as the line item source when available."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)
        selected = [
            {"description": "Scan Processing", "quantity": 10, "unit_price": "5.00"}
        ]

        request = self.factory.get(f"/admin/invoices/{invoice.id}/edit/")
        request.user = user
        request.session = {f"invoice_{invoice.id}_services": selected}

        response = invoice_edit(request, invoice_id=str(invoice.id))

        assert response.status_code == 200


@pytest.mark.django_db()
class TestInvoiceUpdateLineItems:
    """Tests for the invoice_update_line_items AJAX POST endpoint."""

    def setup_method(self) -> None:
        """Set up RequestFactory shared across tests."""
        self.factory = RequestFactory()

    def _post(
        self,
        invoice: Invoice,
        user: object,
        items: list[dict],
        session: dict | None = None,
    ) -> object:
        """Build and dispatch an AJAX POST to invoice_update_line_items.

        Args:
            invoice: Target Invoice instance.
            user: Authenticated request user.
            items: List of line item dicts to submit.
            session: Optional session dict to attach (defaults to empty dict).

        Returns:
            JsonResponse from the view.
        """
        body = json.dumps({"line_items": items})
        request = self.factory.post(
            f"/admin/invoices/{invoice.id}/update-line-items/",
            data=body,
            content_type="application/json",
        )
        request.user = user
        request.session = {} if session is None else session
        return invoice_update_line_items(request, invoice_id=str(invoice.id))

    def test_valid_items_return_correct_financial_totals(self) -> None:
        """Valid items produce correct subtotal, tax_amount, and total_amount."""
        invoice = _create_invoice(
            service_fee=Decimal("0.00"), tax_rate=Decimal("20.00")
        )
        user = UserFactory(is_staff=True, is_superuser=True)
        items = [
            {"description": "Scanning Service", "quantity": 2, "unit_price": 50},
            {"description": "Letter Generation", "quantity": 10, "unit_price": 0.25},
        ]

        response = self._post(invoice, user, items)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True
        summary = data["financial_summary"]
        # subtotal = 2*50 + 10*0.25 = 102.50
        assert summary["subtotal"] == pytest.approx(102.5)
        # tax = 102.50 * 0.20 = 20.50
        assert summary["tax_amount"] == pytest.approx(20.5)
        # total = 102.50 + 20.50 = 123.00
        assert summary["total_amount"] == pytest.approx(123.0)

    def test_empty_items_return_zero_totals(self) -> None:
        """Submitting an empty items list returns success with zero financial totals."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        response = self._post(invoice, user, [])

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["success"] is True
        assert data["financial_summary"]["subtotal"] == 0.0

    def test_missing_description_returns_400(self) -> None:
        """Item with an empty description string is rejected with HTTP 400."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        response = self._post(
            invoice, user, [{"description": "", "quantity": 1, "unit_price": 10}]
        )

        assert response.status_code == 400
        assert "error" in json.loads(response.content)

    def test_negative_quantity_returns_400(self) -> None:
        """Item with a negative quantity is rejected with HTTP 400."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        response = self._post(
            invoice, user, [{"description": "Test", "quantity": -1, "unit_price": 10}]
        )

        assert response.status_code == 400

    def test_negative_unit_price_returns_400(self) -> None:
        """Item with a negative unit price is rejected with HTTP 400."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        response = self._post(
            invoice, user, [{"description": "Test", "quantity": 1, "unit_price": -5}]
        )

        assert response.status_code == 400

    def test_invalid_json_body_returns_400(self) -> None:
        """Malformed JSON request body returns HTTP 400 with error message."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        request = self.factory.post(
            f"/admin/invoices/{invoice.id}/update-line-items/",
            data="not valid json }{",
            content_type="application/json",
        )
        request.user = user
        request.session = {}

        response = invoice_update_line_items(request, invoice_id=str(invoice.id))

        assert response.status_code == 400
        assert json.loads(response.content)["error"] == "Invalid JSON"

    def test_non_list_line_items_returns_400(self) -> None:
        """Sending a dict instead of a list for line_items is rejected with HTTP 400."""
        invoice = _create_invoice()
        user = UserFactory(is_staff=True, is_superuser=True)

        body = json.dumps({"line_items": {"description": "bad format"}})
        request = self.factory.post(
            f"/admin/invoices/{invoice.id}/update-line-items/",
            data=body,
            content_type="application/json",
        )
        request.user = user
        request.session = {}

        response = invoice_update_line_items(request, invoice_id=str(invoice.id))

        assert response.status_code == 400

    def test_custom_items_stored_in_session(self) -> None:
        """Custom line items (is_custom=True) are persisted in session after update."""
        invoice = _create_invoice(tax_rate=Decimal("0.00"))
        user = UserFactory(is_staff=True, is_superuser=True)
        session: dict = {}
        items = [
            {
                "description": "Extra bespoke charge",
                "quantity": 1,
                "unit_price": 25,
                "is_custom": True,
            }
        ]

        self._post(invoice, user, items, session=session)

        key = f"invoice_{invoice.id}_custom_items"
        assert key in session
        assert session[key][0]["description"] == "Extra bespoke charge"

    def test_non_custom_items_not_stored_in_session(self) -> None:
        """Standard (non-custom) items are not persisted to session custom_items key."""
        invoice = _create_invoice(tax_rate=Decimal("0.00"))
        user = UserFactory(is_staff=True, is_superuser=True)
        session: dict = {}
        items = [
            {
                "description": "Scan fee",
                "quantity": 5,
                "unit_price": 0.10,
                "is_custom": False,
            }
        ]

        self._post(invoice, user, items, session=session)

        key = f"invoice_{invoice.id}_custom_items"
        assert session.get(key) == []
