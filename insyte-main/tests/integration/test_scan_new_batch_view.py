"""Integration tests for manual scan batch creation view."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from tests.factories import UserFactory


@pytest.fixture()
def staff_client() -> tuple[Client, Any]:
    """Return authenticated staff client and user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
class TestScanNewBatchView:
    """Verify batch layout selection during manual ingest."""

    @patch("scans.scan_folder.ScanFolderWatcherService.ingest_folder")
    def test_valid_layout_posts_to_ingest_service(
        self,
        mock_ingest: MagicMock,
        staff_client: tuple[Client, Any],
    ) -> None:
        """Valid payment-method/layout combinations are accepted."""
        client, user = staff_client
        mock_ingest.return_value = {
            "status": "ok",
            "file_count": 3,
            "total_batches": 1,
            "batches": [
                {
                    "scan_batch_id": "00000000-0000-0000-0000-000000000001",
                    "batch_name": "BRC SPRING25 CASH",
                    "file_count": 3,
                }
            ],
        }

        response = client.post(
            reverse("custom_admin:scan_new_batch"),
            data={
                "r2_prefix": "ScanOutput/BRC/SPRING25/cash/",
                "appeal_code": "SPRING25",
                "payment_method": "cash",
                "scan_form_type": "duplex",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 200
        mock_ingest.assert_called_once_with(
            appeal_code="SPRING25",
            payment_method="cash",
            scan_form_type="duplex",
            r2_prefix="ScanOutput/BRC/SPRING25/cash/",
            user=user,
            auto_process=True,
        )

    def test_invalid_layout_rejected_before_ingest(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Invalid payment-method/layout combinations are rejected."""
        client, _user = staff_client

        response = client.post(
            reverse("custom_admin:scan_new_batch"),
            data={
                "r2_prefix": "ScanOutput/BRC/SPRING25/cash/",
                "appeal_code": "SPRING25",
                "payment_method": "cash",
                "scan_form_type": "simplex_with_payment",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        assert b"Invalid form layout" in response.content
