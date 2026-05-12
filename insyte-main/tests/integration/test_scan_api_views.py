"""Integration tests for scan-processing API endpoints."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from tests.factories import CampaignFactory, UserFactory


@pytest.fixture()
def staff_client() -> tuple[Client, object]:
    """Return authenticated staff client and user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    admin_group, _ = Group.objects.get_or_create(name="admin")
    user.groups.add(admin_group)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])

    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
class TestScanBatchCreateApi:
    """Validate API contract for scan batch creation."""

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_batch_create_forwards_batch_name(
        self,
        mock_delay: Any,
        staff_client: tuple[Client, object],
    ) -> None:
        """The API should pass the physical batch identifier through to the task."""
        client, user = staff_client
        campaign = CampaignFactory()
        mock_delay.return_value = SimpleNamespace(id="task-123")

        response = client.post(
            reverse("custom_admin:scan_batch_create"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
                    "payment_method": "cheque",
                    "scan_form_type": "simplex_with_payment",
                    "batch_name": "Batch-505.pdf",
                    "auto_process": True,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        mock_delay.assert_called_once_with(
            campaign_id=str(campaign.id),
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            batch_name="Batch-505.pdf",
            user_id=user.id,
            auto_process=True,
        )

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_batch_create_rejects_unknown_campaign(
        self,
        mock_delay: Any,
        staff_client: tuple[Client, object],
    ) -> None:
        """The API should reject unknown campaigns before queuing work."""
        client, _user = staff_client

        response = client.post(
            reverse("custom_admin:scan_batch_create"),
            data=json.dumps(
                {
                    "campaign_id": "00000000-0000-0000-0000-000000000099",
                    "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
                    "payment_method": "cheque",
                    "scan_form_type": "simplex_with_payment",
                    "batch_name": "Batch-505.pdf",
                    "auto_process": True,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 404
        assert response.json() == {"error": "Campaign not found"}
        mock_delay.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_batch_create_requires_payment_method(
        self,
        mock_delay: Any,
        staff_client: tuple[Client, object],
    ) -> None:
        """The API should reject missing payment method instead of defaulting it."""
        client, _user = staff_client

        response = client.post(
            reverse("custom_admin:scan_batch_create"),
            data=json.dumps(
                {
                    "campaign_id": "00000000-0000-0000-0000-000000000001",
                    "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
                    "scan_form_type": "simplex_with_payment",
                    "batch_name": "Batch-505.pdf",
                    "auto_process": True,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json() == {"error": "payment_method is required"}
        mock_delay.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_batch_create_rejects_invalid_payment_layout_combination(
        self,
        mock_delay: Any,
        staff_client: tuple[Client, object],
    ) -> None:
        """The API should reject incompatible payment method/layout combinations."""
        client, _user = staff_client

        response = client.post(
            reverse("custom_admin:scan_batch_create"),
            data=json.dumps(
                {
                    "campaign_id": "00000000-0000-0000-0000-000000000001",
                    "r2_prefix": "ScanOutput/BRC/SPRING25/cash/",
                    "payment_method": "cash",
                    "scan_form_type": "simplex_with_payment",
                    "batch_name": "Batch-505.pdf",
                    "auto_process": True,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "Invalid form layout" in response.json()["error"]
        mock_delay.assert_not_called()
