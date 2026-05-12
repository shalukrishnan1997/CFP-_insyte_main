"""Integration tests for invoice list template wiring."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from invoices.models import Invoice
from tests.factories import CampaignFactory, UserFactory


@pytest.fixture()
def staff_client() -> tuple[Client, object]:
    """Return authenticated client and staff user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
def test_invoice_list_renders_status_action_url_templates(
    staff_client: tuple[Client, object],
) -> None:
    """Invoice list exposes URL-tag-derived status actions in the page source."""
    client, user = staff_client
    campaign = CampaignFactory(created_by=user)
    invoice = Invoice.objects.create(
        client=campaign.client,
        campaign=campaign,
        created_by=user,
        status=Invoice.STATUS_DRAFT,
        billing_period_start=date.today() - timedelta(days=30),
        billing_period_end=date.today(),
        due_date=date.today() + timedelta(days=14),
        service_fee=Decimal("100.00"),
        tax_rate=Decimal("20.00"),
    )

    response = client.get(reverse("custom_admin:invoice_list"))

    assert response.status_code == 200
    body = response.content.decode()
    assert (
        reverse(
            "custom_admin:invoice_mark_paid",
            kwargs={"invoice_id": "00000000-0000-0000-0000-000000000000"},
        )
        in body
    )
    assert (
        reverse(
            "custom_admin:invoice_change_status",
            kwargs={"invoice_id": "00000000-0000-0000-0000-000000000000"},
        )
        in body
    )
    assert str(invoice.id) in body
