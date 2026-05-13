"""Invoice number uniqueness when multiple rows are persisted."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from invoices.models import Invoice
from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db(transaction=True)
class TestInvoiceNumberAllocation:
    """Sequential guard for ``INV-YYYY-MM-xxxx`` prefixes (see InvoiceService)."""

    def test_sequential_creation_produces_unique_numbers(self) -> None:
        user = UserFactory(is_staff=True, is_superuser=True)
        campaign = CampaignFactory(created_by=user)

        ld = timezone.localdate()
        numbers: set[str] = set()
        for _ in range(6):
            inv = Invoice.objects.create(
                client=campaign.client,
                campaign=campaign,
                billing_period_start=ld,
                billing_period_end=ld,
                due_date=ld,
                created_by=user,
                status=Invoice.STATUS_DRAFT,
                service_fee=Decimal("5.00"),
            )
            inv.refresh_from_db()
            numbers.add(inv.invoice_number)

        assert len(numbers) == 6
