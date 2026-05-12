"""Integration tests for Daily Banking filters.

Verifies that the batch_id filter is correctly applied to:
1. The main dashboard aggregates.
2. The flat donation table HTMX partial.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    UserFactory,
)


@pytest.fixture()
def staff_client() -> tuple[Client, Any]:
    """Return authenticated client and staff user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
class TestBankingFilters:
    """Test that batch_id filter works across all banking views."""

    def test_dashboard_filtered_by_batch(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Dashboard totals should only include donations from the selected batch."""
        client, _ = staff_client

        batch1 = DonationBatchFactory()
        batch2 = DonationBatchFactory(campaign=batch1.campaign)

        # approved bankable donations
        DonationFactory(
            batch=batch1,
            campaign=batch1.campaign,
            payment_method="cheque",
            amount=Decimal("100.00"),
            qa_status="approved",
        )
        DonationFactory(
            batch=batch2,
            campaign=batch1.campaign,
            payment_method="cheque",
            amount=Decimal("50.00"),
            qa_status="approved",
        )

        # unfiltered dashboard
        url = reverse("custom_admin:daily_banking_dashboard")
        response = client.get(url)
        assert response.context["total_unassigned"] >= 2

        # Filtered by batch1
        response = client.get(f"{url}?batch_id={batch1.id}")
        assert response.context["total_unassigned"] == 1
        assert response.context["total_amount"] == Decimal("100.00")

    def test_banking_batch_queue_htmx_filtered_by_batch(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """HTMX batch queue partial should only list batches with eligible donations in scope."""
        client_obj, _ = staff_client

        camp = CampaignFactory()
        batch1 = DonationBatchFactory(campaign=camp)
        batch2 = DonationBatchFactory(campaign=camp)

        DonationFactory(
            batch=batch1,
            campaign=camp,
            payment_method="cheque",
            amount=Decimal("100.00"),
            qa_status="approved",
        )
        DonationFactory(
            batch=batch2,
            campaign=camp,
            payment_method="cheque",
            amount=Decimal("50.00"),
            qa_status="approved",
        )

        url = reverse("custom_admin:htmx_banking_batch_queue")

        response = client_obj.get(url)
        batches = list(response.context["banking_batches"])
        assert batch1 in batches
        assert batch2 in batches

        response = client_obj.get(f"{url}?batch_id={batch1.id}")
        batches_filtered = list(response.context["banking_batches"])
        assert batch1 in batches_filtered
        assert batch2 not in batches_filtered

    def test_dashboard_donation_queue_filtered_by_batch(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Donation-level queue respects batch filter as well."""
        client, _ = staff_client
        camp = CampaignFactory()
        batch1 = DonationBatchFactory(campaign=camp)
        batch2 = DonationBatchFactory(campaign=camp)
        donation1 = DonationFactory(
            batch=batch1,
            campaign=camp,
            payment_method="cheque",
            amount=Decimal("25.00"),
            qa_status="approved",
        )
        donation2 = DonationFactory(
            batch=batch2,
            campaign=camp,
            payment_method="cheque",
            amount=Decimal("35.00"),
            qa_status="approved",
        )

        url = reverse("custom_admin:daily_banking_dashboard")
        response = client.get(f"{url}?batch_id={batch1.id}")
        rows = list(response.context["dashboard_donations"])

        assert donation1 in rows
        assert donation2 not in rows
