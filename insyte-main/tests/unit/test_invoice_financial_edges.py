"""Unit tests for invoice financial edge cases and precision guards."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from invoices.models import Invoice, InvoiceSettings
from invoices.utils import calculate_all_line_totals, get_invoice_line_items
from tests.factories import CampaignFactory, UserFactory


def _create_invoice(
    *,
    service_fee: Decimal = Decimal("0.00"),
    processing_fee: Decimal = Decimal("0.00"),
    setup_fee: Decimal = Decimal("0.00"),
    additional_charges: Decimal = Decimal("0.00"),
    discount_amount: Decimal = Decimal("0.00"),
    tax_rate: Decimal = Decimal("0.00"),
    status: str = Invoice.STATUS_DRAFT,
    due_date: date | None = None,
) -> Invoice:
    """Create a minimal invoice for financial edge tests."""
    user = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory(created_by=user)
    return Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        billing_period_start=date.today() - timedelta(days=30),
        billing_period_end=date.today(),
        due_date=due_date or (date.today() + timedelta(days=30)),
        created_by=user,
        status=status,
        service_fee=service_fee,
        processing_fee=processing_fee,
        setup_fee=setup_fee,
        additional_charges=additional_charges,
        discount_amount=discount_amount,
        tax_rate=tax_rate,
    )


@pytest.mark.django_db()
class TestInvoiceFinancialEdgeCases:
    """Regression tests for precision and financial guard behavior."""

    def test_tax_amount_rounds_to_two_decimal_places(self) -> None:
        """Tax and total are persisted with expected currency precision."""
        invoice = _create_invoice(
            service_fee=Decimal("10.05"),
            tax_rate=Decimal("17.50"),
        )

        invoice.refresh_from_db()
        assert invoice.subtotal == Decimal("10.05")
        assert invoice.tax_amount == Decimal("1.76")
        assert invoice.total_amount == Decimal("11.81")

    def test_zero_total_invoice_not_marked_paid(self) -> None:
        """Zero-value invoices remain in draft and do not auto-transition to paid."""
        invoice = _create_invoice()
        invoice.amount_paid = Decimal("0.00")
        invoice.save()
        invoice.refresh_from_db()

        assert invoice.total_amount == Decimal("0.00")
        assert invoice.status == Invoice.STATUS_DRAFT

    def test_get_invoice_line_items_excludes_zero_quantities(self) -> None:
        """Line-item helper only returns billable metrics with quantity above zero."""
        invoice = _create_invoice()
        invoice.total_donations_captured = 2
        invoice.gift_aid_captured = 0
        invoice.campaigns_created = 0
        invoice.letters_generated = 1
        invoice.save()

        settings = InvoiceSettings.get_settings()
        line_items = get_invoice_line_items(invoice, settings)
        descriptions = {item[0] for item in line_items}

        assert "Donations Captured" in descriptions
        assert "Thank You Letters Generated" in descriptions
        assert "Gift Aid Declarations" not in descriptions
        assert "Campaigns Created" not in descriptions

    def test_line_totals_support_unsaved_settings_float_defaults(self) -> None:
        """Line total calculation handles unsaved InvoiceSettings numeric defaults."""
        invoice = _create_invoice()
        invoice.storage_used_mb = Decimal("1.50")
        invoice.database_queries = 10
        invoice.save()

        unsaved_settings = InvoiceSettings()
        line_totals = calculate_all_line_totals(invoice, unsaved_settings)

        assert line_totals["storage_line_total"] == Decimal("0.0150")
        assert line_totals["db_query_line_total"] == Decimal("0.010")
