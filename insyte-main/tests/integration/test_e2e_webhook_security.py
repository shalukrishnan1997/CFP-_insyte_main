"""End-to-end webhook security integration tests.

Drives the ``/webhooks/scan-upload/`` endpoint against four hardening
contracts:

1. **HMAC mismatch → 403** — a request with a wrong signature is rejected
   before any side-effect (no DB write, no Celery task).
2. **Replay (same nonce) → 401** — once the scanner workstation has sent
   a request with a given ``(client_id, nonce)``, any second request that
   reuses the same nonce within the 5-minute window is rejected outright.
   This sits *above* the 24-hour completion-hash dedup so a captured-and-
   replayed request can't masquerade as a legitimate retry.
3. **Legitimate retry without nonce → 409 (duplicate_batch)** — when the
   scanner retries a complete-status webhook with no anti-replay nonce
   *and* the same ``(campaign, batch_name)`` collides with an existing
   ``ScanBatch``, the webhook surfaces a 409 with an actionable hint
   instead of a 500.
4. **PDF page overflow → 413** — when the R2 prefix the webhook
   references contains a PDF whose page count exceeds
   ``MAX_OCR_PAGES_PER_PDF``, the webhook rejects with 413 and never
   enqueues the OCR Celery task. Backed by ``tests/fixtures/000015.pdf``
   (64-page PDF) with a temporarily-lowered limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from tests.factories import (
    CampaignFactory,
    ClientFactory,
    ScanBatchFactory,
)

_SCAN_SECRET = "scan-e2e-secret"
_FIXTURE_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Return HMAC-SHA256 hex digest over ``f"{timestamp}.{payload}"``.

    Args:
        secret: Shared secret configured as ``SCAN_WEBHOOK_SECRET``.
        payload: Raw request body bytes.
        timestamp: ``X-Scan-Timestamp`` header value, prepended to the
            signed bytes (separated by a literal ``.``).

    Returns:
        Hex-encoded HMAC-SHA256 digest.
    """
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


@pytest.fixture()
def _scan_secret(settings: SettingsWrapper) -> None:
    """Configure the scanner webhook HMAC secret for the duration of a test."""
    settings.SCAN_WEBHOOK_SECRET = _SCAN_SECRET
    cache.clear()


def _signed_payload(
    payload_dict: dict[str, Any], *, timestamp: int | None = None
) -> tuple[bytes, str, str]:
    """Serialize ``payload_dict`` and return ``(body, signature, timestamp_str)``.

    Args:
        payload_dict: JSON-serializable webhook payload.
        timestamp: Optional unix timestamp; defaults to current wall-clock.

    Returns:
        3-tuple ``(payload_bytes, hex_signature, timestamp_str)`` — caller
        posts ``body`` with headers ``X-Signature: <hex_signature>`` and
        ``X-Scan-Timestamp: <timestamp_str>``.
    """
    payload = json.dumps(payload_dict).encode()
    ts_str = str(timestamp if timestamp is not None else int(time.time()))
    signature = _scan_signature(_SCAN_SECRET, payload, ts_str)
    return payload, signature, ts_str


@pytest.mark.django_db()
class TestWebhookHmacMismatch:
    """A request with a tampered HMAC must be rejected before any side-effect."""

    def test_hmac_mismatch_rejected(self, client: Client, _scan_secret: None) -> None:
        """A bad signature returns 403 and never enqueues the OCR task."""
        selected_client = ClientFactory(name="HMAC Mismatch Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict: dict[str, Any] = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": "ScanOutput/hmac-mismatch/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-hmac-mismatch.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        # Sign with the wrong secret to force the verification path to fail.
        bad_signature = _scan_signature("not-the-real-secret", payload, ts_str)

        with patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay:
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=bad_signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert response.status_code == 403
        assert response.json()["error"] == "Invalid signature"
        mock_delay.assert_not_called()


@pytest.mark.django_db()
class TestWebhookNonceReplay:
    """Replayed requests with the same nonce must be rejected with 401."""

    def test_replayed_nonce_returns_401(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Second POST with same ``(client_id, nonce)`` returns 401 outright."""
        selected_client = ClientFactory(name="Nonce Replay Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict: dict[str, Any] = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": "ScanOutput/nonce-replay/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-nonce-replay.pdf",
            "timestamp": int(timezone.now().timestamp()),
            "nonce": uuid.uuid4().hex,
        }
        payload, signature, ts_str = _signed_payload(payload_dict)

        with patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay:
            mock_delay.return_value = SimpleNamespace(id="ocr-task-nonce-replay")

            first = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )
            second = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert first.status_code == 200
        assert second.status_code == 401
        assert second.json()["error"] == "replayed nonce"
        # The OCR pipeline must have fired exactly once (on the first request).
        assert mock_delay.call_count == 1


@pytest.mark.django_db()
class TestWebhookLegitimateRetryDedup:
    """A legitimate retry without a nonce that hits a duplicate batch returns 409."""

    def test_duplicate_batch_returns_409_with_hint(
        self, client: Client, _scan_secret: None
    ) -> None:
        """``(campaign, batch_name)`` collision returns 409 and not 500."""
        selected_client = ClientFactory(name="Dedup Retry Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        existing_name = "Batch-legit-retry.pdf"
        ScanBatchFactory(campaign=campaign, batch_name=existing_name)

        payload_dict: dict[str, Any] = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": "ScanOutput/legit-retry/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": existing_name,
        }
        payload, signature, ts_str = _signed_payload(payload_dict)

        with patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay:
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "duplicate_batch"
        assert "unique filename" in body["hint"]
        # We rejected before queueing the OCR task.
        mock_delay.assert_not_called()


@pytest.mark.django_db()
class TestWebhookPdfPageOverflow:
    """A PDF over the configured page limit returns 413, not 200/500."""

    def test_oversize_pdf_returns_413(
        self,
        client: Client,
        settings: SettingsWrapper,
        _scan_secret: None,
    ) -> None:
        """A 64-page real PDF with limit lowered to 10 returns 413."""
        # Real PDF fixture exists and has 64 pages — set the cap to 10 so
        # the page-overflow branch fires deterministically.
        assert _FIXTURE_PDF.exists(), (
            f"Fixture missing: {_FIXTURE_PDF}. Run "
            "`cp 000015.pdf tests/fixtures/000015.pdf`."
        )
        settings.MAX_OCR_PAGES_PER_PDF = 10

        selected_client = ClientFactory(name="PDF Overflow Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        r2_prefix = "ScanOutput/pdf-overflow/cheque/"
        pdf_key = f"{r2_prefix}000015.pdf"
        pdf_bytes = _FIXTURE_PDF.read_bytes()

        payload_dict: dict[str, Any] = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": r2_prefix,
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "000015.pdf",
        }
        payload, signature, ts_str = _signed_payload(payload_dict)

        with (
            patch(
                "scans.scan_folder._list_all_keys_under_prefix",
                return_value=[pdf_key],
            ),
            patch("core.storage_backends.r2_enabled", return_value=True),
            patch("core.storage_backends.get_r2_client") as mock_r2,
            patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay,
        ):
            mock_r2.return_value.get_object.return_value = {
                "Body": MagicMock(read=lambda: pdf_bytes)
            }
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert response.status_code == 413
        body = response.json()
        assert "exceeds MAX_OCR_PAGES_PER_PDF=10" in body["error"]
        assert "000015.pdf" in body["error"]
        # OCR task must NOT have been enqueued.
        mock_delay.assert_not_called()
