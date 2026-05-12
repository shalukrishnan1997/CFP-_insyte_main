"""Integration tests for Scan Retry API views.

SCAN-RETRY-INT-* test cases covering rate limiting, authentication, and responses.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from tests.factories import ScanBatchFactory, ScanPlaceholderFactory, UserFactory


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """Clear the cache before every test to isolate rate limits."""
    cache.clear()


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
class TestScanRetryViews:
    """SCAN-RETRY-INT-001 to 007: Retry endpoint tests."""

    @patch("scans.tasks.retry_failed_scans_task.delay")
    def test_retry_failed_valid_batch(
        self, mock_delay: MagicMock, staff_client: tuple[Client, Any]
    ) -> None:
        """SCAN-RETRY-INT-001: POST valid batch -> 200 OK & triggers Celery."""
        client, _user = staff_client
        batch = ScanBatchFactory()
        mock_delay.return_value.id = "task-retry-failed-001"

        url = reverse("custom_admin:scan_retry_failed")
        response = client.post(
            url,
            data={"scan_batch_id": str(batch.id)},
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_delay.assert_called_once_with(str(batch.id))

    def test_retry_failed_unauthenticated(self) -> None:
        """SCAN-RETRY-INT-002: Anonymous user -> 302 login redirect."""
        client = Client()
        batch = ScanBatchFactory()
        url = reverse("custom_admin:scan_retry_failed")

        response = client.post(
            url,
            data={"scan_batch_id": str(batch.id)},
            content_type="application/json",
        )
        assert response.status_code in [301, 302]

    def test_retry_failed_non_staff(self) -> None:
        """SCAN-RETRY-INT-003: Non-staff user -> Denied."""
        user = UserFactory(is_staff=False, username="regular-user")
        client = Client()
        client.force_login(user)

        batch = ScanBatchFactory()
        url = reverse("custom_admin:scan_retry_failed")

        response = client.post(
            url,
            data={"scan_batch_id": str(batch.id)},
            content_type="application/json",
        )
        assert response.status_code in [301, 302, 403]

    def test_retry_failed_missing_id(self, staff_client: tuple[Client, Any]) -> None:
        """SCAN-RETRY-INT-004: Missing scan_batch_id -> 400."""
        client, _ = staff_client
        url = reverse("custom_admin:scan_retry_failed")

        response = client.post(
            url,
            data={},
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.json()
        assert "scan_batch_id is required" in data["error"]

    @patch("scans.tasks.retry_failed_scans_task.delay")
    def test_retry_failed_rate_limit(
        self, mock_delay: MagicMock, staff_client: tuple[Client, Any]
    ) -> None:
        """SCAN-RETRY-INT-005: Second immediate retry -> 429 rate limited."""
        client, _ = staff_client
        batch = ScanBatchFactory()
        url = reverse("custom_admin:scan_retry_failed")
        mock_delay.return_value.id = "task-retry-failed-002"

        payload = {"scan_batch_id": str(batch.id)}

        # First request should succeed
        resp1 = client.post(url, data=payload, content_type="application/json")
        assert resp1.status_code == 200
        mock_delay.assert_called_once()

        # Second request immediately should be 429
        resp2 = client.post(url, data=payload, content_type="application/json")
        assert resp2.status_code == 429
        data = resp2.json()
        assert data["rate_limited"] is True
        assert (
            "cooldown" in data.get("error", "").lower()
            or "wait" in data.get("error", "").lower()
        )

    @patch("scans.tasks.process_single_scan_task.delay")
    def test_retry_single_valid_placeholder(
        self, mock_delay: MagicMock, staff_client: tuple[Client, Any]
    ) -> None:
        """SCAN-RETRY-INT-006: POST valid placeholder -> 200 OK & triggers Celery."""
        client, _ = staff_client
        ph = ScanPlaceholderFactory()
        mock_delay.return_value.id = "task-retry-single-001"

        url = reverse("custom_admin:scan_retry_single")
        response = client.post(
            url,
            data={"placeholder_id": str(ph.id)},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_delay.assert_called_once_with(str(ph.id))

    def test_retry_single_missing_id(self, staff_client: tuple[Client, Any]) -> None:
        """SCAN-RETRY-INT-007: Missing placeholder_id -> 400."""
        client, _ = staff_client
        url = reverse("custom_admin:scan_retry_single")

        response = client.post(
            url,
            data={},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "placeholder_id is required" in response.json()["error"]
