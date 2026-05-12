"""Unit tests for core.services.invoice — InvoiceService."""

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from tests.factories import ClientFactory


def _make_invoice(client: Any, payment_status: str = "unpaid") -> Any:
    """Create a minimal Invoice-like object (mocked) to avoid full Invoice FK setup."""
    from invoices.models import Invoice

    invoice = MagicMock(spec=Invoice)
    invoice.invoice_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
    invoice.payment_status = payment_status
    invoice.status = "issued"
    invoice.payment_date = None
    invoice.payment_method = ""
    invoice.payment_reference = ""
    invoice.amount_paid = Decimal("0.00")
    invoice.balance_due = Decimal("100.00")
    invoice.client = client
    return invoice


def _make_payment(amount: Decimal = Decimal("100.00")) -> Any:
    from payments.models import StripePayment

    payment = MagicMock(spec=StripePayment)
    payment.stripe_payment_intent_id = "pi_test_123456"
    payment.amount = amount
    return payment


@pytest.mark.django_db()
class TestInvoiceServiceMarkAsPaid:
    """Tests for InvoiceService.mark_as_paid."""

    def test_mark_as_paid_updates_fields(self) -> None:
        from invoices.services import InvoiceService

        client = ClientFactory()
        invoice = _make_invoice(client)
        payment = _make_payment()

        InvoiceService.mark_as_paid(invoice, payment)

        assert invoice.payment_status == "paid"
        assert invoice.status == "paid"  # Invoice.STATUS_PAID = "paid"
        assert invoice.payment_method == "credit_card"
        assert invoice.payment_reference == "pi_test_123456"
        assert invoice.amount_paid == Decimal("100.00")
        assert invoice.balance_due == 0

    def test_mark_as_paid_sets_payment_date(self) -> None:
        from invoices.services import InvoiceService

        client = ClientFactory()
        invoice = _make_invoice(client)
        payment = _make_payment()

        before = timezone.now().date()
        InvoiceService.mark_as_paid(invoice, payment)
        after = timezone.now().date()

        assert before <= invoice.payment_date <= after

    def test_mark_as_paid_saves_invoice(self) -> None:
        from invoices.services import InvoiceService

        client = ClientFactory()
        invoice = _make_invoice(client)
        payment = _make_payment()

        InvoiceService.mark_as_paid(invoice, payment)

        invoice.save.assert_called_once()


@pytest.mark.django_db()
class TestInvoiceServiceSendReceiptEmail:
    """Tests for InvoiceService.send_receipt_email."""

    @patch("invoices.services.render_to_string")
    def test_returns_true_on_success(self, mock_render: MagicMock) -> None:
        from invoices.services import InvoiceService

        mock_render.return_value = "<html>Receipt</html>"
        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)
        payment = _make_payment()

        with patch("django.core.mail.EmailMultiAlternatives") as mock_email_cls:
            mock_email = MagicMock()
            mock_email_cls.return_value = mock_email
            result = InvoiceService.send_receipt_email(invoice, payment)

        assert result is True
        mock_email.send.assert_called_once()

    @patch("invoices.services.render_to_string")
    def test_returns_false_on_email_exception(self, mock_render: MagicMock) -> None:
        from invoices.services import InvoiceService

        mock_render.return_value = "<html>Receipt</html>"
        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)
        payment = _make_payment()

        with patch("django.core.mail.EmailMultiAlternatives") as mock_email_cls:
            mock_email = MagicMock()
            mock_email.send.side_effect = Exception("SMTP error")
            mock_email_cls.return_value = mock_email
            result = InvoiceService.send_receipt_email(invoice, payment)

        assert result is False

    @patch(
        "invoices.services.render_to_string",
        side_effect=Exception("Template not found"),
    )
    def test_returns_false_on_template_error(self, _mock_render: MagicMock) -> None:
        from invoices.services import InvoiceService

        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)
        payment = _make_payment()

        result = InvoiceService.send_receipt_email(invoice, payment)
        assert result is False


@pytest.mark.django_db()
class TestInvoiceServiceSendPaymentFailedEmail:
    """Tests for InvoiceService.send_payment_failed_email."""

    @patch("invoices.services.render_to_string")
    def test_returns_true_on_success(self, mock_render: MagicMock) -> None:
        from invoices.services import InvoiceService

        mock_render.return_value = "<html>Payment Failed</html>"
        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)

        with patch("django.core.mail.EmailMultiAlternatives") as mock_email_cls:
            mock_email = MagicMock()
            mock_email_cls.return_value = mock_email
            result = InvoiceService.send_payment_failed_email(invoice, "Card declined")

        assert result is True
        mock_email.send.assert_called_once()

    @patch("invoices.services.render_to_string")
    def test_returns_false_on_exception(self, mock_render: MagicMock) -> None:
        from invoices.services import InvoiceService

        mock_render.return_value = "<html>Payment Failed</html>"
        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)

        with patch("django.core.mail.EmailMultiAlternatives") as mock_email_cls:
            mock_email = MagicMock()
            mock_email.send.side_effect = Exception("SMTP unreachable")
            mock_email_cls.return_value = mock_email
            result = InvoiceService.send_payment_failed_email(
                invoice, "Connection timeout"
            )

        assert result is False

    @patch("invoices.services.render_to_string")
    def test_email_subject_contains_invoice_number(
        self, mock_render: MagicMock
    ) -> None:
        from invoices.services import InvoiceService

        mock_render.return_value = "<html>Payment Failed</html>"
        client = ClientFactory(email="client@example.com")
        invoice = _make_invoice(client)
        invoice.invoice_number = "INV-TESTNUM"

        with patch("django.core.mail.EmailMultiAlternatives") as mock_email_cls:
            mock_email = MagicMock()
            mock_email_cls.return_value = mock_email
            InvoiceService.send_payment_failed_email(invoice, "Card declined")

        call_kwargs = mock_email_cls.call_args
        assert "INV-TESTNUM" in call_kwargs[1]["subject"]
