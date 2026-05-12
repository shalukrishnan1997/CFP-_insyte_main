"""Unit tests for the 'Send by email' invoice admin action."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from audit.models import AuditLog
from invoices.forms import InvoiceSendEmailForm
from invoices.models import Invoice
from invoices.services import InvoiceService
from tests.factories import CampaignFactory, UserFactory


def _create_invoice(
    *,
    service_fee: Decimal = Decimal("100.00"),
    tax_rate: Decimal = Decimal("20.00"),
    status: str = Invoice.STATUS_DRAFT,
) -> Invoice:
    """Persist a minimal Invoice for send-email tests."""
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
class TestInvoiceSendEmailForm:
    """Tests for InvoiceSendEmailForm validation."""

    def test_valid_form_with_to_only(self) -> None:
        form = InvoiceSendEmailForm(data={"to": "client@example.com"})
        assert form.is_valid()
        assert form.cleaned_data["to"] == ["client@example.com"]
        assert form.cleaned_data["cc"] == []

    def test_valid_form_with_to_cc_message(self) -> None:
        form = InvoiceSendEmailForm(
            data={
                "to": "a@example.com, b@example.com",
                "cc": "cc1@example.com",
                "message": "Hello",
            }
        )
        assert form.is_valid()
        assert form.cleaned_data["to"] == ["a@example.com", "b@example.com"]
        assert form.cleaned_data["cc"] == ["cc1@example.com"]
        assert form.cleaned_data["message"] == "Hello"

    def test_empty_to_is_invalid(self) -> None:
        form = InvoiceSendEmailForm(data={"to": ""})
        assert not form.is_valid()
        assert "to" in form.errors

    def test_invalid_to_address_rejected(self) -> None:
        form = InvoiceSendEmailForm(data={"to": "not-an-email"})
        assert not form.is_valid()
        assert "to" in form.errors

    def test_invalid_cc_address_rejected(self) -> None:
        form = InvoiceSendEmailForm(data={"to": "ok@example.com", "cc": "bad@@example"})
        assert not form.is_valid()
        assert "cc" in form.errors


@pytest.mark.django_db()
class TestSendInvoiceByEmailService:
    """Tests for InvoiceService.send_invoice_by_email."""

    def setup_method(self) -> None:
        mail.outbox = []

    def test_sends_email_with_pdf_attachment_and_recipients(self) -> None:
        invoice = _create_invoice()
        sender = UserFactory(is_staff=True)

        InvoiceService.send_invoice_by_email(
            invoice,
            to=["a@example.com"],
            cc=["b@example.com"],
            body="Operator note here",
            sender=sender,
        )

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["a@example.com"]
        assert message.cc == ["b@example.com"]
        assert invoice.invoice_number in message.subject
        assert "Operator note here" in message.body
        assert len(message.attachments) == 1
        attachment_name, _attachment_content, attachment_mime = message.attachments[0]
        assert attachment_name == f"invoice_{invoice.invoice_number}.pdf"
        assert attachment_mime == "application/pdf"

    def test_logs_audit_entry_on_send(self) -> None:
        invoice = _create_invoice()
        sender = UserFactory(is_staff=True)

        InvoiceService.send_invoice_by_email(
            invoice,
            to=["audit@example.com"],
            sender=sender,
        )

        log = AuditLog.objects.filter(
            model_name="Invoice", object_id=str(invoice.id), action="EMAIL"
        ).first()
        assert log is not None
        assert "audit@example.com" in log.summary
        assert log.user_id == sender.id

    def test_empty_to_raises_validation_error(self) -> None:
        invoice = _create_invoice()
        sender = UserFactory(is_staff=True)

        with pytest.raises(ValidationError):
            InvoiceService.send_invoice_by_email(invoice, to=[], sender=sender)
        assert mail.outbox == []

    def test_invalid_email_raises_validation_error(self) -> None:
        invoice = _create_invoice()
        sender = UserFactory(is_staff=True)

        with pytest.raises(ValidationError):
            InvoiceService.send_invoice_by_email(
                invoice, to=["not-an-email"], sender=sender
            )
        assert mail.outbox == []

    def test_invalid_cc_raises_validation_error(self) -> None:
        invoice = _create_invoice()
        sender = UserFactory(is_staff=True)

        with pytest.raises(ValidationError):
            InvoiceService.send_invoice_by_email(
                invoice,
                to=["ok@example.com"],
                cc=["bad@@example"],
                sender=sender,
            )
        assert mail.outbox == []


@pytest.mark.django_db()
class TestInvoiceSendEmailView:
    """Tests for the invoice_send_email_view HTTP view."""

    def setup_method(self) -> None:
        mail.outbox = []
        self.client = Client()

    def test_anonymous_user_redirected(self) -> None:
        invoice = _create_invoice()
        url = reverse("custom_admin:invoice_send_email", args=[str(invoice.id)])
        response = self.client.get(url)
        assert response.status_code == 302

    def test_staff_get_renders_form(self) -> None:
        invoice = _create_invoice()
        staff = UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        url = reverse("custom_admin:invoice_send_email", args=[str(invoice.id)])

        response = self.client.get(url)

        assert response.status_code == 200
        assert b"Send Invoice by Email" in response.content
        # Client primary contact email is pre-filled in the "To" field.
        assert invoice.client.email
        assert invoice.client.email.encode() in response.content

    def test_staff_post_sends_email_and_redirects(self) -> None:
        invoice = _create_invoice()
        staff = UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        url = reverse("custom_admin:invoice_send_email", args=[str(invoice.id)])

        response = self.client.post(
            url,
            data={
                "to": "client@example.com",
                "cc": "",
                "message": "Please find attached.",
            },
            follow=False,
        )

        assert response.status_code == 302
        assert response.url == reverse(
            "custom_admin:invoice_detail", args=[str(invoice.id)]
        )
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["client@example.com"]

    def test_staff_post_invalid_to_renders_form_with_errors(self) -> None:
        invoice = _create_invoice()
        staff = UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        url = reverse("custom_admin:invoice_send_email", args=[str(invoice.id)])

        response = self.client.post(
            url,
            data={"to": "not-an-email", "cc": "", "message": ""},
        )

        assert response.status_code == 200
        assert mail.outbox == []
