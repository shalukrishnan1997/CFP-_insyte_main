"""Robustness tests for the scan-upload webhook.

Covers the three vulnerabilities patched in this unit:

1. **Replay protection** — duplicate ``(client_id, timestamp)`` within 5 min
   is rejected, and timestamps outside the 60s skew window are rejected.
2. **Duplicate batch handling** — ``(campaign, batch_name)`` collision returns
   HTTP 409 with ``{"error": "duplicate_batch"}`` instead of 500.
3. **Per-client rate limit** — 60 requests / minute, 61st returns 429.
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from tests.factories import (
    CampaignFactory,
    ClientFactory,
    ScanBatchFactory,
)


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Compute the HMAC-SHA256 signature the same way the production helper does."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _post_scan_webhook(
    client: Client,
    payload_dict: dict[str, Any],
    *,
    secret: str = "scan-secret-test",
    timestamp: int,
) -> Any:
    """Helper: serialize, sign, and POST a payload to the scan-upload webhook."""
    payload = json.dumps(payload_dict).encode()
    ts_str = str(timestamp)
    signature = _scan_signature(secret, payload, ts_str)
    return client.post(
        "/webhooks/scan-upload/",
        data=payload,
        content_type="application/json",
        HTTP_X_SIGNATURE=signature,
        HTTP_X_SCAN_TIMESTAMP=ts_str,
    )


@pytest.mark.django_db()
class TestScanWebhookReplayProtection:
    """Replay protection on the (client_id, timestamp) tuple."""

    @pytest.fixture(autouse=True)
    def _setup(self, settings: SettingsWrapper) -> None:
        settings.SCAN_WEBHOOK_SECRET = "scan-secret-test"
        # Always-on replay protection — only requires the test to send a ts.
        settings.SCAN_WEBHOOK_TIMESTAMP_REQUIRED = False
        cache.clear()

    def test_same_timestamp_replayed_within_window_returns_401(
        self, client: Client
    ) -> None:
        """Second request with same (client_id, timestamp) is rejected."""
        selected_client = ClientFactory(name="Replay Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ts = int(time.time())

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 5,
            "latest_urn": "IMG_001.tiff",
            "status": "scanning",
        }

        first = _post_scan_webhook(client, payload_dict, timestamp=ts)
        assert first.status_code == 200, first.content

        second = _post_scan_webhook(client, payload_dict, timestamp=ts)
        assert second.status_code == 401
        assert second.json()["error"] == "replayed timestamp"

    def test_stale_timestamp_returns_401(self, client: Client) -> None:
        """Timestamps more than 60s in the past are rejected."""
        selected_client = ClientFactory(name="Stale Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ts = int(time.time()) - 5 * 60  # 5 minutes old

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "status": "scanning",
        }

        response = _post_scan_webhook(client, payload_dict, timestamp=ts)
        assert response.status_code == 401
        assert "skew" in response.json()["error"]

    def test_future_timestamp_returns_401(self, client: Client) -> None:
        """Timestamps more than 60s in the future are rejected."""
        selected_client = ClientFactory(name="Future Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ts = int(time.time()) + 5 * 60  # 5 minutes ahead

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "status": "scanning",
        }

        response = _post_scan_webhook(client, payload_dict, timestamp=ts)
        assert response.status_code == 401

    def test_missing_timestamp_rejected(self, client: Client) -> None:
        """A request without ``X-Scan-Timestamp`` cannot produce a valid
        signature and is rejected with 403."""
        selected_client = ClientFactory(name="No-Timestamp Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        # Best-effort signature without a timestamp — must still be rejected
        # because the server requires the timestamp-prefixed signing form.
        signature = hmac.new(b"scan-secret-test", payload, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
        )
        assert response.status_code == 403

    def test_signature_with_wrong_timestamp_rejected(self, client: Client) -> None:
        """Tampering with the timestamp invalidates the signature."""
        selected_client = ClientFactory(name="Tamper Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ts = int(time.time())
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        # Sign with one timestamp, send a different one in the header.
        signature = _scan_signature("scan-secret-test", payload, str(ts))
        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=str(ts + 1),
        )
        assert response.status_code == 403


@pytest.mark.django_db()
class TestScanWebhookDuplicateBatch:
    """``(campaign, batch_name)`` unique-constraint collision returns 409."""

    @pytest.fixture(autouse=True)
    def _setup(self, settings: SettingsWrapper) -> None:
        settings.SCAN_WEBHOOK_SECRET = "scan-secret-test"
        cache.clear()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_duplicate_batch_name_returns_409(
        self, mock_delay: MagicMock, client: Client
    ) -> None:
        """A repeated batch_name on the same campaign yields 409 + hint."""
        selected_client = ClientFactory(name="Dup Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        # Pre-existing ScanBatch with the same (campaign, batch_name).
        existing_name = "Batch-001.pdf"
        ScanBatchFactory(campaign=campaign, batch_name=existing_name)

        ts = int(time.time())
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "latest_urn": existing_name,
            "status": "complete",
            "r2_prefix": "ScanOutput/dup/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": existing_name,
        }

        response = _post_scan_webhook(client, payload_dict, timestamp=ts)

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "duplicate_batch"
        assert "unique filename" in body["hint"]
        # We rejected before queueing the OCR task.
        mock_delay.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_unique_batch_name_succeeds(
        self, mock_delay: MagicMock, client: Client
    ) -> None:
        """A fresh batch_name still queues OCR normally — sanity check."""
        mock_delay.return_value = SimpleNamespace(id="ocr-task-fresh")
        selected_client = ClientFactory(name="Fresh Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ts = int(time.time())
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "latest_urn": "fresh.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/fresh/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "fresh.pdf",
        }

        response = _post_scan_webhook(client, payload_dict, timestamp=ts)

        assert response.status_code == 200
        mock_delay.assert_called_once()


@pytest.mark.django_db()
class TestScanWebhookRateLimit:
    """Per-client rate limit: 60 requests/minute, 61st returns 429."""

    @pytest.fixture(autouse=True)
    def _setup(self, settings: SettingsWrapper) -> None:
        settings.SCAN_WEBHOOK_SECRET = "scan-secret-test"
        cache.clear()

    def test_61_requests_in_a_minute_returns_429(self, client: Client) -> None:
        """The 61st request from one client in a minute is rate-limited."""
        selected_client = ClientFactory(name="Chatty Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        # Pin time so all 61 requests land in the same minute bucket. We use
        # a unique timestamp per request so the replay check doesn't kick in
        # before the rate limiter does. Patch core.webhooks.time.time so the
        # rate-limit bucket stays fixed even on slow CI runners where the 61
        # sequential POSTs can take >60s wall-clock and roll the bucket over.
        base_ts = int(time.time())

        with patch("core.webhooks.time.time", return_value=float(base_ts)):
            for i in range(60):
                payload_dict = {
                    "campaign_id": str(campaign.id),
                    "client_id": str(selected_client.id),
                    "total_uploaded": i,
                    "total_expected": 100,
                    "latest_urn": f"IMG_{i:03d}.tiff",
                    "status": "scanning",
                }
                response = _post_scan_webhook(
                    client, payload_dict, timestamp=base_ts + i
                )
                assert response.status_code == 200, (
                    f"req {i} failed: {response.status_code} {response.content!r}"
                )

            # The 61st request must be rejected with 429 before doing any work.
            payload_dict = {
                "campaign_id": str(campaign.id),
                "client_id": str(selected_client.id),
                "total_uploaded": 61,
                "total_expected": 100,
                "latest_urn": "IMG_061.tiff",
                "status": "scanning",
            }
            response = _post_scan_webhook(client, payload_dict, timestamp=base_ts + 60)
            assert response.status_code == 429
            assert response.json()["error"] == "rate limit exceeded"

    def test_other_client_unaffected_by_neighbour_rate_limit(
        self, client: Client
    ) -> None:
        """Rate limit is per-client_id — sibling clients each get a fresh budget."""
        chatty = ClientFactory(name="Loud Client")
        quiet = ClientFactory(name="Quiet Client")
        chatty_campaign = CampaignFactory(client=chatty, status="active")
        quiet_campaign = CampaignFactory(client=quiet, status="active")
        base_ts = int(time.time())

        # Patch wall clock so the bucket stays fixed across all 61 sequential
        # POSTs even on slow CI runners.
        with patch("core.webhooks.time.time", return_value=float(base_ts)):
            # Burn the chatty client's quota.
            for i in range(60):
                response = _post_scan_webhook(
                    client,
                    {
                        "campaign_id": str(chatty_campaign.id),
                        "client_id": str(chatty.id),
                        "status": "scanning",
                    },
                    timestamp=base_ts + i,
                )
                assert response.status_code == 200

            # Quiet client's first request still succeeds.
            response = _post_scan_webhook(
                client,
                {
                    "campaign_id": str(quiet_campaign.id),
                    "client_id": str(quiet.id),
                    "status": "scanning",
                },
                timestamp=base_ts,
            )
            assert response.status_code == 200
