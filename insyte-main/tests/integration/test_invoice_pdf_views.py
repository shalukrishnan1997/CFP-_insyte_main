"""Unit tests for invoice PDF generation and fallback behavior."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import RequestFactory

from invoices.admin_views import _pdf_response
from invoices.models import Invoice
from tests.factories import CampaignFactory, UserFactory


def _create_invoice() -> Invoice:
    """Create a minimal invoice suitable for PDF-response tests."""
    user = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory(created_by=user)
    return Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        billing_period_start=date.today() - timedelta(days=30),
        billing_period_end=date.today(),
        due_date=date.today() + timedelta(days=30),
        created_by=user,
        service_fee=Decimal("10.00"),
        tax_rate=Decimal("20.00"),
    )


@pytest.mark.django_db()
class TestInvoicePdfViews:
    """Regression tests for invoice PDF download/preview code paths."""

    def test_pdf_download_returns_pdf_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Download mode returns PDF bytes with content-disposition header."""
        invoice = _create_invoice()
        request = RequestFactory().get(f"/admin/invoices/{invoice.id}/pdf/")
        request.user = invoice.created_by
        request.session = {}

        monkeypatch.setattr(
            "invoices.admin_views.InvoicePdfService.build_pdf",
            lambda *_args, **_kwargs: b"%PDF-1.4 mocked-pdf",
        )
        monkeypatch.setattr(
            "invoices.admin_views.calculate_all_line_totals",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(
            "invoices.admin_views.InvoicePdfService.get_content_disposition",
            lambda _invoice, mode: f'{mode}; filename="invoice-test.pdf"',
        )
        monkeypatch.setattr(
            "invoices.admin_views.log_request_action",
            lambda *_args, **_kwargs: None,
        )

        response = _pdf_response(request, str(invoice.id), mode="download")

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert (
            response["Content-Disposition"] == 'download; filename="invoice-test.pdf"'
        )
        assert response.content.startswith(b"%PDF-1.4")

    def test_pdf_preview_falls_back_to_html_on_generation_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preview mode falls back to HTML when PDF generation raises an error."""
        invoice = _create_invoice()
        request = RequestFactory().get(f"/admin/invoices/{invoice.id}/preview/")
        request.user = invoice.created_by
        request.session = {}

        monkeypatch.setattr(
            "invoices.admin_views.InvoicePdfService.build_pdf",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pdf-failed")),
        )
        monkeypatch.setattr(
            "invoices.admin_views.calculate_all_line_totals",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(
            "invoices.admin_views.render_to_string",
            lambda *_args, **_kwargs: "<html><body>invoice fallback</body></html>",
        )

        captured_messages: list[str] = []
        monkeypatch.setattr(
            "invoices.admin_views.messages.info",
            lambda _request, message: captured_messages.append(str(message)),
        )

        response = _pdf_response(request, str(invoice.id), mode="preview")

        assert response.status_code == 200
        assert response["Content-Type"] == "text/html"
        assert "invoice fallback" in response.content.decode()
        assert any("preview error" in message for message in captured_messages)
