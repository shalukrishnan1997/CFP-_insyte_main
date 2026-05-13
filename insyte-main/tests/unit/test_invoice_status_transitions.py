"""Unit tests for invoice status transition behavior."""

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from invoices.admin_views import (
    invoice_change_status,
    invoice_detail,
    invoice_mark_paid,
    invoice_metrics_api,
)
from invoices.models import Invoice
from tests.factories import CampaignFactory, ClientFactory, UserFactory


def _create_invoice(
    *,
    service_fee: Decimal = Decimal("100.00"),
    tax_rate: Decimal = Decimal("0.00"),
    status: str = Invoice.STATUS_DRAFT,
    due_date: date | None = None,
) -> Invoice:
    """Create a minimal invoice for transition tests."""
    user = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory(created_by=user)
    return Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        billing_period_start=timezone.localdate() - timedelta(days=30),
        billing_period_end=timezone.localdate(),
        due_date=due_date or (timezone.localdate() + timedelta(days=30)),
        created_by=user,
        status=status,
        service_fee=service_fee,
        tax_rate=tax_rate,
    )


@pytest.mark.django_db()
class TestInvoiceModelStatusTransitions:
    """Tests for model-level invoice status transitions on save."""

    def test_invoice_becomes_paid_when_amount_paid_reaches_total(self) -> None:
        """Invoice transitions to paid when full amount is paid."""
        invoice = _create_invoice(service_fee=Decimal("125.00"))

        invoice.amount_paid = invoice.total_amount
        invoice.save()
        invoice.refresh_from_db()

        assert invoice.status == Invoice.STATUS_PAID
        assert invoice.balance_due == Decimal("0.00")

    def test_issued_invoice_becomes_overdue_when_due_date_passed(self) -> None:
        """Issued invoice with outstanding balance transitions to overdue on save."""
        past = timezone.localdate() - timedelta(days=1)
        invoice = _create_invoice(
            service_fee=Decimal("80.00"),
            status=Invoice.STATUS_ISSUED,
            due_date=past,
        )

        invoice.refresh_from_db()

        assert invoice.status == Invoice.STATUS_OVERDUE
        assert invoice.balance_due > Decimal("0.00")

        invoice.save()
        invoice.refresh_from_db()

        assert invoice.status == Invoice.STATUS_OVERDUE
        assert invoice.balance_due > Decimal("0.00")


@pytest.mark.django_db()
class TestInvoiceStatusEndpoints:
    """Tests for admin invoice status update endpoints."""

    def test_invoice_detail_uses_session_selected_services(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invoice detail exposes the session-backed service selection in context."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        invoice = _create_invoice(service_fee=Decimal("150.00"))
        captured_context: dict[str, Any] = {}

        monkeypatch.setattr(
            "invoices.admin_views.log_request_action",
            lambda *_args, **_kwargs: None,
        )

        def _capture_render(
            _request: HttpRequest,
            _template_name: str,
            context: dict[str, Any],
        ) -> HttpResponse:
            captured_context.update(context)
            return HttpResponse("ok")

        monkeypatch.setattr("invoices.admin_views.render", _capture_render)

        request = RequestFactory().get(f"/admin/invoices/{invoice.id}/")
        request.user = staff_user
        request.session = {
            f"invoice_{invoice.id}_services": [
                {
                    "description": "Manual Data Cleanup",
                    "quantity": 2,
                    "unit_price": "12.50",
                }
            ]
        }

        response = invoice_detail(request, invoice_id=str(invoice.id))

        assert response.status_code == 200
        assert captured_context["has_selected_services"] is True
        assert captured_context["selected_services"] == [
            ("Manual Data Cleanup", 2, Decimal("12.50"), Decimal("25.00"))
        ]
        assert captured_context["invoice"] == invoice

    def test_invoice_metrics_api_rejects_cross_client_campaign(self) -> None:
        """Metrics API rejects campaign IDs that do not belong to the client."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        client = ClientFactory()
        other_campaign = CampaignFactory()

        request = RequestFactory().get(
            "/admin/invoices/metrics/",
            {
                "client_id": str(client.id),
                "campaign_id": str(other_campaign.id),
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
        request.user = staff_user

        response = invoice_metrics_api(request)

        assert response.status_code == 400
        assert json.loads(response.content) == {
            "error": "Selected campaign does not belong to the selected client."
        }

    def test_invoice_metrics_api_returns_not_found_for_missing_client(
        self,
    ) -> None:
        """Metrics API returns 404 when the requested client does not exist."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)

        request = RequestFactory().get(
            "/admin/invoices/metrics/",
            {
                "client_id": "00000000-0000-0000-0000-000000000000",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
        request.user = staff_user

        response = invoice_metrics_api(request)

        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "Client or campaign not found"}

    def test_invoice_change_status_rejects_invalid_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Status-change endpoint rejects unknown status values."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        invoice = _create_invoice()
        monkeypatch.setattr(
            "invoices.admin_views.log_request_action",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr("invoices.admin_views.messages.error", lambda *_args: None)

        request = RequestFactory().post(
            f"/admin/invoices/{invoice.id}/change-status/",
            {"status": "not-a-real-status"},
        )
        request.user = staff_user

        response = invoice_change_status(request, invoice_id=str(invoice.id))

        invoice.refresh_from_db()
        assert response.status_code == 302
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_invoice_change_status_updates_to_issued(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Status-change endpoint updates invoice when status is valid."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        invoice = _create_invoice(status=Invoice.STATUS_DRAFT)
        monkeypatch.setattr(
            "invoices.admin_views.log_request_action",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "invoices.admin_views.messages.success",
            lambda *_args: None,
        )

        request = RequestFactory().post(
            f"/admin/invoices/{invoice.id}/change-status/",
            {"status": Invoice.STATUS_ISSUED},
        )
        request.user = staff_user

        response = invoice_change_status(request, invoice_id=str(invoice.id))

        invoice.refresh_from_db()
        assert response.status_code == 302
        assert invoice.status == Invoice.STATUS_ISSUED

    def test_invoice_mark_paid_sets_paid_status_and_balance_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mark-paid endpoint records payment and marks invoice paid."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        invoice = _create_invoice(service_fee=Decimal("150.00"))
        monkeypatch.setattr(
            "invoices.admin_views.log_request_action",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "invoices.admin_views.messages.success",
            lambda *_args: None,
        )

        request = RequestFactory().post(
            f"/admin/invoices/{invoice.id}/mark-paid/",
            {
                "amount": str(invoice.total_amount),
                "payment_method": "cheque",
                "payment_reference": "TXN-001",
                "payment_date": str(date.today()),
            },
        )
        request.user = staff_user

        response = invoice_mark_paid(request, invoice_id=str(invoice.id))

        invoice.refresh_from_db()
        assert response.status_code == 302
        assert invoice.status == Invoice.STATUS_PAID
        assert invoice.balance_due == Decimal("0.00")
        assert invoice.payment_reference == "TXN-001"
