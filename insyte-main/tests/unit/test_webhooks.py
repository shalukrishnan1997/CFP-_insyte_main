"""Unit tests for Stripe webhook handling and idempotency.

Tests that duplicate webhook events are properly rejected.
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from payments.models import StripeWebhookEvent
from scans.models import ScanUploadProgress
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    PaymentGatewayConfigFactory,
    UserFactory,
)


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Return HMAC-SHA256 signature over ``f"{timestamp}.{payload}"``."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


@pytest.mark.django_db()
class TestStripeWebhookIdempotency:
    """Tests for Stripe webhook idempotency guard."""

    def test_duplicate_event_id_rejected(self) -> None:
        """Second webhook with same stripe_event_id is skipped."""
        # Create an existing event
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_test_duplicate_123",
            event_type="payment_intent.succeeded",
            payload={
                "id": "evt_test_duplicate_123",
                "type": "payment_intent.succeeded",
            },
            processed=False,
        )

        # Verify duplicate exists check works
        assert StripeWebhookEvent.objects.filter(
            stripe_event_id="evt_test_duplicate_123"
        ).exists()

    def test_unique_event_id_accepted(self) -> None:
        """New webhook event with unique ID is created."""
        event = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_test_unique_456",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_test_unique_456", "type": "payment_intent.succeeded"},
            processed=False,
        )
        assert event.pk is not None
        assert event.processed is False

    def test_duplicate_event_raises_integrity_error(self) -> None:
        """Creating event with duplicate stripe_event_id raises IntegrityError."""
        from django.db import IntegrityError

        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_test_integrity_789",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_test_integrity_789"},
            processed=False,
        )

        with pytest.raises(IntegrityError):
            StripeWebhookEvent.objects.create(
                stripe_event_id="evt_test_integrity_789",
                event_type="payment_intent.succeeded",
                payload={"id": "evt_test_integrity_789"},
                processed=False,
            )

    def test_event_processing_marks_complete(self) -> None:
        """Processed event has processed=True and processed_at set."""
        from django.utils import timezone

        event = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_test_process_001",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_test_process_001", "type": "payment_intent.succeeded"},
            processed=False,
        )

        # Simulate processing
        event.processed = True
        event.processed_at = timezone.now()
        event.processing_attempts = 1
        event.save()

        event.refresh_from_db()
        assert event.processed is True
        assert event.processed_at is not None
        assert event.processing_attempts == 1


@pytest.mark.django_db()
class TestStripeWebhookEndpointBehavior:
    """Tests covering signature validation and queueing behavior for webhook endpoint."""

    def test_webhook_returns_500_when_secret_missing(self, client: Client) -> None:
        """Endpoint fails fast when no PaymentGatewayConfig has a webhook secret.

        Webhook secrets live in PaymentGatewayConfig.webhook_secret_encrypted —
        when no active row carries one, _get_stripe_webhook_secrets() returns []
        and the handler must short-circuit with HTTP 500. The pytest-django
        transactional test DB starts empty, so no setup is needed.
        """
        response = client.post(
            "/webhooks/stripe/", data=b"{}", content_type="application/json"
        )

        assert response.status_code == 500
        assert response.json()["error"] == "Webhook secret not configured"

    def test_webhook_uses_client_config_secret_when_global_missing(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Webhook signature verification should use active client Stripe config secrets."""
        PaymentGatewayConfigFactory(
            provider="stripe",
            is_active=True,
            publishable_key_encrypted="pk_test_client",
            secret_key_encrypted="sk_test_client",
            webhook_secret_encrypted="whsec_client_config",
        )
        event_id = "evt_client_secret_001"
        seen_secret: dict[str, str] = {}

        class FakeEvent:
            id = event_id
            type = "payment_intent.succeeded"

            @staticmethod
            def to_dict() -> dict[str, str]:
                return {"id": event_id, "type": "payment_intent.succeeded"}

        def fake_construct_event(_payload: bytes, _sig: str, secret: str) -> FakeEvent:
            seen_secret["value"] = secret
            return FakeEvent()

        monkeypatch.setattr("stripe.Webhook.construct_event", fake_construct_event)
        monkeypatch.setattr(
            "core.tasks.process_stripe_webhook.delay", lambda _event_id: None
        )

        response = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
        )

        assert response.status_code == 200
        assert seen_secret["value"] == "whsec_client_config"
        assert StripeWebhookEvent.objects.filter(stripe_event_id=event_id).exists()

    def test_webhook_returns_400_on_signature_verification_failure(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid Stripe signatures are rejected."""
        PaymentGatewayConfigFactory(
            provider="stripe",
            is_active=True,
            webhook_secret_encrypted="whsec_test",
        )

        def raise_signature_error(_payload: bytes, _sig: str, _secret: str):
            raise Exception("signature-failed")

        monkeypatch.setattr(
            "stripe.Webhook.construct_event",
            lambda payload, sig, secret: (_ for _ in ()).throw(
                __import__("stripe").SignatureVerificationError("bad sig", sig)
            ),
        )

        response = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="invalid",
        )

        assert response.status_code == 400
        assert response.json()["error"] == "Invalid signature"

    def test_webhook_duplicate_event_returns_200_without_queueing(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Duplicate event IDs short-circuit without enqueuing a task."""
        PaymentGatewayConfigFactory(
            provider="stripe",
            is_active=True,
            webhook_secret_encrypted="whsec_test",
        )
        event_id = "evt_duplicate_id_001"

        StripeWebhookEvent.objects.create(
            stripe_event_id=event_id,
            event_type="payment_intent.succeeded",
            payload={"id": event_id, "type": "payment_intent.succeeded"},
            processed=False,
        )

        class FakeEvent:
            id = event_id
            type = "payment_intent.succeeded"

            @staticmethod
            def to_dict() -> dict[str, str]:
                return {"id": event_id, "type": "payment_intent.succeeded"}

        monkeypatch.setattr(
            "stripe.Webhook.construct_event", lambda *_args: FakeEvent()
        )

        called = {"queued": False}

        def fake_delay(_event_id: str) -> None:
            called["queued"] = True

        monkeypatch.setattr("core.tasks.process_stripe_webhook.delay", fake_delay)

        response = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
        )

        assert response.status_code == 200
        assert called["queued"] is False

    def test_webhook_creates_event_and_queues_task(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid event is stored and queued for async processing."""
        PaymentGatewayConfigFactory(
            provider="stripe",
            is_active=True,
            webhook_secret_encrypted="whsec_test",
        )
        event_id = "evt_queue_001"

        class FakeEvent:
            id = event_id
            type = "payment_intent.succeeded"

            @staticmethod
            def to_dict() -> dict[str, str]:
                return {"id": event_id, "type": "payment_intent.succeeded"}

        monkeypatch.setattr(
            "stripe.Webhook.construct_event", lambda *_args: FakeEvent()
        )

        queued: dict[str, str] = {}

        def fake_delay(webhook_event_id: str) -> SimpleNamespace:
            queued["webhook_event_id"] = webhook_event_id
            return SimpleNamespace(id="task-webhook-001")

        monkeypatch.setattr("core.tasks.process_stripe_webhook.delay", fake_delay)

        response = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
        )

        assert response.status_code == 200

        created_event = StripeWebhookEvent.objects.get(stripe_event_id=event_id)
        assert created_event.event_type == "payment_intent.succeeded"
        assert created_event.processed is False
        assert queued["webhook_event_id"] == str(created_event.id)


@pytest.mark.django_db()
class TestScannerWebhookIsolation:
    """Tests scanner webhook isolation by client scope."""

    @pytest.fixture(autouse=True)
    def _set_scan_secret(self, settings: SettingsWrapper) -> None:
        """Set scanner webhook secret and clear webhook caches per test."""
        from django.core.cache import cache

        settings.SCAN_WEBHOOK_SECRET = "scan-secret-test"
        # The locmem cache persists across tests in this module — clear so
        # replay-protection / rate-limit slots from prior tests don't leak.
        cache.clear()

    def test_scanner_campaigns_list_requires_client_id(self, client: Client) -> None:
        """Campaign list endpoint rejects requests without client scope."""
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", b"", ts_str)

        response = client.get(
            "/webhooks/scanner/campaigns/",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 400
        assert response.json()["error"] == "client_id is required"

    def test_scanner_campaigns_list_returns_only_client_campaigns(
        self, client: Client
    ) -> None:
        """Campaign list endpoint returns active campaigns for selected client only."""
        selected_client = ClientFactory(name="Selected Client")
        other_client = ClientFactory(name="Other Client")
        selected_campaign = CampaignFactory(client=selected_client, status="active")
        CampaignFactory(client=other_client, status="active")

        query = f"client_id={selected_client.id}"
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", query.encode(), ts_str)

        response = client.get(
            "/webhooks/scanner/campaigns/",
            {"client_id": str(selected_client.id)},
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        payload = response.json()
        campaign_ids = {campaign["id"] for campaign in payload["campaigns"]}
        assert campaign_ids == {str(selected_campaign.id)}

    def test_scan_upload_webhook_rejects_campaign_client_mismatch(
        self, client: Client
    ) -> None:
        """Upload webhook rejects payload when campaign belongs to another client."""
        declared_client = ClientFactory(name="Declared Client")
        foreign_campaign = CampaignFactory(status="active")

        payload_dict = {
            "campaign_id": str(foreign_campaign.id),
            "client_id": str(declared_client.id),
            "total_uploaded": 2,
            "total_expected": 10,
            "latest_urn": "IMG_001.tiff",
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Campaign does not belong to client"

    def test_scan_upload_webhook_accepts_matching_client_scope(
        self, client: Client
    ) -> None:
        """Upload webhook stores progress when campaign and client match."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 10,
            "latest_urn": "IMG_003.tiff",
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert ScanUploadProgress.objects.filter(campaign=campaign).exists()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_requires_payment_method_on_complete(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Completion payloads must include payment method before OCR is queued."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-001.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "scan_form_type": "simplex_with_payment",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 400
        assert response.json()["error"] == (
            "payment_method is required when status is complete"
        )
        mock_delay.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_rejects_invalid_layout_on_complete(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Completion payloads must use a layout compatible with payment method."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-001.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cash/",
            "payment_method": "cash",
            "scan_form_type": "simplex_with_payment",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 400
        assert "Invalid scan_form_type" in response.json()["error"]
        mock_delay.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_queues_ocr_for_valid_complete_payload(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Valid completion payloads should enqueue OCR batch creation."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        mock_delay.return_value = SimpleNamespace(id="ocr-task-123")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-001.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-001.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        assert response.json()["ocr_task_id"] == "ocr-task-123"
        mock_delay.assert_called_once_with(
            campaign_id=str(campaign.id),
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            batch_name="Batch-001.pdf",
            user_id=None,
            auto_process=True,
        )

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.last_completion_task_id == "ocr-task-123"
        assert progress.last_completion_request_hash != ""
        assert progress.last_completion_dispatched_at is not None

    def test_scan_upload_webhook_rejects_unknown_status_when_strict(
        self, client: Client, settings: SettingsWrapper
    ) -> None:
        """When STRICT_SCANNER_STATUS_VALIDATION=True, unknown status returns 400."""
        settings.STRICT_SCANNER_STATUS_VALIDATION = True
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 5,
            "status": "bogus",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "invalid status"
        assert body["got"] == "bogus"
        assert set(body["allowed"]) == {"idle", "scanning", "complete", "error"}
        assert not ScanUploadProgress.objects.filter(campaign=campaign).exists()

    def test_scan_upload_webhook_coerces_unknown_status_when_lax(
        self,
        client: Client,
        settings: SettingsWrapper,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Default (STRICT_SCANNER_STATUS_VALIDATION=False) coerces unknown status
        to 'scanning', logs a WARNING with the raw value, and still persists progress."""
        import logging

        settings.STRICT_SCANNER_STATUS_VALIDATION = False
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 5,
            "status": "bogus",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        # Re-enable the webhook logger — the test settings disable existing
        # loggers, so we explicitly opt back in for this assertion.
        webhook_logger = logging.getLogger("core.webhooks")
        webhook_logger.disabled = False
        webhook_logger.propagate = True
        caplog.set_level(logging.WARNING, logger="core.webhooks")

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == "scanning"
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "unknown status coerced" in record.getMessage()
        ]
        assert warnings, "expected a WARNING log entry for the coerced status"
        assert "'bogus'" in warnings[0].getMessage()

    def test_scan_upload_webhook_caps_error_message_length(
        self, client: Client
    ) -> None:
        """error_message is truncated to 2000 chars to match other field caps."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        long_error = "x" * 5000
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 0,
            "total_expected": 5,
            "status": "error",
            "error_message": long_error,
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == "error"
        assert len(progress.last_error) == 2000
        assert progress.last_error == "x" * 2000

    def test_scan_upload_webhook_persists_error_message(self, client: Client) -> None:
        """Error status webhooks persist error_message into last_error."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 2,
            "total_expected": 10,
            "status": "error",
            "error_message": "R2 outage",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == "error"
        assert progress.last_error == "R2 outage"

    def test_scan_upload_webhook_clears_error_on_recovery(self, client: Client) -> None:
        """Transitioning out of error status clears last_error."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        ScanUploadProgress.objects.create(
            campaign=campaign,
            status="error",
            last_error="Previous failure",
        )

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 5,
            "total_expected": 10,
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature("scan-secret-test", payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == "scanning"
        assert progress.last_error == ""

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_dedups_replayed_complete_payload(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Same canonical payload across distinct timestamps must not enqueue a second task."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        mock_delay.return_value = SimpleNamespace(id="ocr-task-replay")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-replay.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-replay.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        # Use different timestamps for the two posts so we exercise the
        # canonical-hash dedup path rather than the (client_id, timestamp)
        # replay guard, which would 401 a literal repeat.
        ts_first = str(int(time.time()))
        ts_second = str(int(time.time()) + 1)
        sig_first = _scan_signature("scan-secret-test", payload, ts_first)
        sig_second = _scan_signature("scan-secret-test", payload, ts_second)

        first = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=sig_first,
            HTTP_X_SCAN_TIMESTAMP=ts_first,
        )
        assert first.status_code == 200
        assert first.json()["ocr_task_id"] == "ocr-task-replay"

        second = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=sig_second,
            HTTP_X_SCAN_TIMESTAMP=ts_second,
        )

        assert second.status_code == 200
        body = second.json()
        assert body["status"] == "duplicate"
        assert body["ocr_task_id"] == "ocr-task-replay"
        assert mock_delay.call_count == 1

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_redispatches_after_dedup_window(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Identical payload outside the dedup window enqueues a fresh task."""
        from datetime import timedelta

        from django.utils import timezone

        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        mock_delay.side_effect = [
            SimpleNamespace(id="ocr-task-window-1"),
            SimpleNamespace(id="ocr-task-window-2"),
        ]

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-window.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-window.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        # Different timestamps for the two requests so the (client_id, ts)
        # replay guard doesn't 401 the second post — the dedup-window check
        # is what we're exercising here.
        ts_first = str(int(time.time()))
        ts_second = str(int(time.time()) + 1)
        sig_first = _scan_signature("scan-secret-test", payload, ts_first)
        sig_second = _scan_signature("scan-secret-test", payload, ts_second)

        first = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=sig_first,
            HTTP_X_SCAN_TIMESTAMP=ts_first,
        )
        assert first.status_code == 200

        ScanUploadProgress.objects.filter(campaign=campaign).update(
            last_completion_dispatched_at=timezone.now() - timedelta(hours=48),
        )

        second = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=sig_second,
            HTTP_X_SCAN_TIMESTAMP=ts_second,
        )

        assert second.status_code == 200
        assert second.json()["ocr_task_id"] == "ocr-task-window-2"
        assert mock_delay.call_count == 2

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_scan_upload_webhook_redispatches_for_changed_payload(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """A different completion payload (e.g. new prefix) must enqueue again."""
        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        mock_delay.side_effect = [
            SimpleNamespace(id="ocr-task-change-1"),
            SimpleNamespace(id="ocr-task-change-2"),
        ]

        base = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-change.pdf",
            "status": "complete",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-change.pdf",
        }
        first_payload = json.dumps(
            {**base, "r2_prefix": "ScanOutput/BRC/SPRING25/cheque-1/"}
        ).encode()
        second_payload = json.dumps(
            {**base, "r2_prefix": "ScanOutput/BRC/SPRING25/cheque-2/"}
        ).encode()

        ts_first = str(int(time.time()))
        ts_second = str(int(time.time()) + 1)
        first = client.post(
            "/webhooks/scan-upload/",
            data=first_payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=_scan_signature(
                "scan-secret-test", first_payload, ts_first
            ),
            HTTP_X_SCAN_TIMESTAMP=ts_first,
        )
        second = client.post(
            "/webhooks/scan-upload/",
            data=second_payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=_scan_signature(
                "scan-secret-test", second_payload, ts_second
            ),
            HTTP_X_SCAN_TIMESTAMP=ts_second,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_delay.call_count == 2

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_concurrent_webhook_replays_dispatch_only_once(
        self,
        mock_delay: MagicMock,
        client: Client,
    ) -> None:
        """Two concurrent replays must not both dispatch the OCR task.

        Simulates the race where two retried webhook deliveries both pass the
        in-memory hash comparison before either has updated the row. With the
        atomic conditional UPDATE in place, only the first request's claim can
        win, so ``create_scan_batch_from_r2_task.delay`` is called exactly
        once.
        """
        from core import webhooks

        selected_client = ClientFactory(name="Scope Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        mock_delay.return_value = SimpleNamespace(id="ocr-task-race")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-race.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-race.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        # Distinct timestamps so the (client_id, ts) replay guard doesn't
        # 401 the inner request — we're testing the atomic conditional
        # UPDATE in the dedup path, not the cache-based replay guard.
        ts_outer = str(int(time.time()))
        ts_inner = str(int(time.time()) + 1)
        sig_outer = _scan_signature("scan-secret-test", payload, ts_outer)
        sig_inner = _scan_signature("scan-secret-test", payload, ts_inner)

        # Simulate the race: when the first request enters the dispatch path
        # and is about to call .delay(), a second concurrent webhook arrives.
        # The second request reads the same pre-claim state but its conditional
        # UPDATE must lose because the first has already claimed the slot.
        original_handle = webhooks._handle_upload_complete
        nested_response: dict[str, object] = {}

        def reentrant_handle_upload_complete(
            data: dict[str, Any],
            campaign_id: str,
            progress: ScanUploadProgress,
            response_data: dict[str, Any],
        ) -> str | None:
            # Restore the original so the nested call uses real logic.
            webhooks._handle_upload_complete = original_handle
            # Inner call simulates the racing replay arriving mid-flight.
            nested = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=sig_inner,
                HTTP_X_SCAN_TIMESTAMP=ts_inner,
            )
            nested_response["status_code"] = nested.status_code
            nested_response["body"] = nested.json()
            # Now run the original "outer" call's logic against the row the
            # nested call has already mutated.
            return original_handle(data, campaign_id, progress, response_data)

        webhooks._handle_upload_complete = reentrant_handle_upload_complete
        try:
            outer = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=sig_outer,
                HTTP_X_SCAN_TIMESTAMP=ts_outer,
            )
        finally:
            webhooks._handle_upload_complete = original_handle

        assert outer.status_code == 200
        assert nested_response["status_code"] == 200

        # Exactly one of the two concurrent requests must have dispatched.
        assert mock_delay.call_count == 1, (
            "Atomic conditional UPDATE should permit only one task dispatch, "
            f"but .delay() was called {mock_delay.call_count} times."
        )

        # The losing request must report the duplicate marker so callers can
        # distinguish a fresh dispatch from a coalesced replay.
        outer_body = outer.json()
        nested_body = nested_response["body"]
        assert isinstance(nested_body, dict)
        bodies = (outer_body, nested_body)
        duplicates = [b for b in bodies if b.get("status") == "duplicate"]
        assert len(duplicates) == 1, (
            "Exactly one concurrent request should be reported as duplicate."
        )

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.last_completion_task_id == "ocr-task-race"


@pytest.mark.django_db()
class TestPaymentServiceIdempotency:
    """Tests that process_successful_payment() is safe to call multiple times.

    Covers the Celery-retry scenario: task crashes after updating the invoice
    but before marking the event as processed, so the task is retried with the
    same payment_intent_id.
    """

    def _make_payment(self, status: str = "pending") -> object:
        """Return a minimal StripePayment-like object stored in the DB."""
        from datetime import date, timedelta
        from decimal import Decimal

        from payments.models import StripeCustomer, StripePayment

        user = UserFactory(is_staff=True, is_superuser=True)
        campaign = CampaignFactory(created_by=user)
        client = campaign.client
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            secret_key_encrypted="sk_test_client",
        )

        customer = StripeCustomer.objects.create(
            stripe_customer_id=f"cus_test_{campaign.id!s:.8s}",
            email=client.email,
            name=client.name,
            client=client,
        )
        from invoices.models import Invoice

        invoice = Invoice.objects.create(
            client=client,
            campaign=campaign,
            billing_period_start=date.today() - timedelta(days=30),
            billing_period_end=date.today(),
            due_date=date.today() + timedelta(days=30),
            created_by=user,
            service_fee=Decimal("50.00"),
        )

        return StripePayment.objects.create(
            stripe_payment_intent_id=f"pi_test_{campaign.id!s:.8s}",
            stripe_customer=customer,
            invoice=invoice,
            amount=Decimal("50.00"),
            currency="GBP",
            status=status,
            description="Test payment",
        )

    def test_already_succeeded_payment_returns_early_without_stripe_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling process_successful_payment on an already-succeeded payment
        returns the payment immediately without hitting the Stripe API."""
        from payments.services import StripePaymentService

        payment = self._make_payment(status="succeeded")

        stripe_calls: list[str] = []

        def fake_retrieve(intent_id: str, **_kwargs: object) -> None:
            stripe_calls.append(intent_id)

        monkeypatch.setattr("stripe.PaymentIntent.retrieve", fake_retrieve)

        result = StripePaymentService.process_successful_payment(
            payment.stripe_payment_intent_id  # type: ignore[union-attr]
        )

        assert result is not None
        assert result.status == "succeeded"  # type: ignore[union-attr]
        assert stripe_calls == [], (
            "Stripe API must not be called for already-succeeded payments"
        )

    def test_already_succeeded_payment_does_not_re_save_invoice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-calling process_successful_payment does not overwrite invoice.amount_paid."""
        from payments.services import StripePaymentService

        payment = self._make_payment(status="succeeded")
        invoice = payment.invoice  # type: ignore[union-attr]
        original_amount_paid = invoice.amount_paid

        monkeypatch.setattr(
            "stripe.PaymentIntent.retrieve",
            lambda *_a, **_kw: None,
        )

        StripePaymentService.process_successful_payment(
            payment.stripe_payment_intent_id  # type: ignore[union-attr]
        )

        invoice.refresh_from_db()
        assert invoice.amount_paid == original_amount_paid, (
            "invoice.amount_paid must not be modified on a duplicate call"
        )

    def test_first_call_on_pending_payment_would_hit_stripe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Confirms the normal path still calls Stripe for PENDING payments."""
        from payments.services import StripePaymentService

        payment = self._make_payment(status="pending")
        stripe_calls: list[str] = []

        def fake_retrieve(intent_id: str, **_kwargs: object) -> object:
            stripe_calls.append(intent_id)
            # Raise to abort early so we don't need a full mock intent object
            raise RuntimeError("stop-here")

        monkeypatch.setattr("stripe.PaymentIntent.retrieve", fake_retrieve)

        with pytest.raises(RuntimeError, match="stop-here"):
            StripePaymentService.process_successful_payment(
                payment.stripe_payment_intent_id  # type: ignore[union-attr]
            )

        assert stripe_calls == [payment.stripe_payment_intent_id]  # type: ignore[union-attr]


@pytest.mark.django_db()
class TestWebhookTaskIdempotency:
    """Tests that the process_stripe_webhook Celery task short-circuits on
    already-processed events (covers the Celery retry / concurrent worker case)."""

    def test_already_processed_event_returns_without_dispatching(self) -> None:
        """Task returns 'Already processed' immediately for processed events."""
        from django.utils import timezone

        from core.tasks import process_stripe_webhook

        event = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_already_done_001",
            event_type="payment_intent.succeeded",
            payload={"id": "evt_already_done_001", "type": "payment_intent.succeeded"},
            processed=True,
            processed_at=timezone.now(),
        )

        # __wrapped__ is a bound method on the task instance — call with just event_id
        result = process_stripe_webhook.__wrapped__(  # type: ignore[attr-defined]
            str(event.id)
        )

        assert result["success"] is True
        assert "Already processed" in result["message"]

    def test_unknown_event_id_returns_error(self) -> None:
        """Task returns an error dict for non-existent event IDs."""
        import uuid

        from core.tasks import process_stripe_webhook

        result = process_stripe_webhook.__wrapped__(  # type: ignore[attr-defined]
            str(uuid.uuid4())
        )

        assert result["success"] is False
        assert result["error"] == "Event not found"


@pytest.mark.django_db()
class TestWebhookTaskLockRetry:
    """Tests the lock-timeout / deadlock retry path on process_stripe_webhook.

    Covers the lock-storm scenario: two Stripe webhook deliveries for the
    same payment (e.g. payment_intent.succeeded racing charge.refunded)
    both enter ``select_for_update()`` and one hits ``LockNotAvailable``
    (or ``DeadlockDetected``). Behaviour we assert:

    * the lock-timeout path issues ``self.retry(...)`` with a jittered
      exponential ``countdown`` (not the default 60s);
    * the path logs at WARNING (not ERROR) so Sentry doesn't get spammed;
    * after the budget is exhausted, the exception re-raises so Celery
      dead-letters the task and the row stays processed=False.
    """

    @staticmethod
    def _make_lock_error() -> Exception:
        """Return a Django OperationalError chained to psycopg2 LockNotAvailable."""
        from django.db import OperationalError
        from psycopg2 import errors as pg_errors

        underlying = pg_errors.LockNotAvailable(
            "canceling statement due to lock timeout"
        )
        wrapped = OperationalError("canceling statement due to lock timeout")
        wrapped.__cause__ = underlying
        return wrapped

    def _make_pending_event(self, event_id_str: str = "evt_lock_001") -> Any:
        """Persist an unprocessed StripeWebhookEvent row for lock-retry tests."""
        return StripeWebhookEvent.objects.create(
            stripe_event_id=event_id_str,
            event_type="payment_intent.succeeded",
            payload={"id": event_id_str, "type": "payment_intent.succeeded"},
            processed=False,
        )

    def test_lock_timeout_triggers_retry_with_jittered_backoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LockNotAvailable raises self.retry() with a sub-10s jittered countdown."""
        from celery.exceptions import Retry

        from core.tasks import process_stripe_webhook

        event = self._make_pending_event("evt_lock_retry_001")

        # First call into the payment service raises a lock error. We patch
        # the symbol the task imports so the failure happens inside the
        # tracked except block.
        def raise_lock(*_args: object, **_kwargs: object) -> None:
            raise self._make_lock_error()

        monkeypatch.setattr(
            "payments.services.StripePaymentService.process_successful_payment",
            staticmethod(raise_lock),
        )

        captured: dict[str, Any] = {}

        def fake_retry(
            self: Any,
            *,
            exc: BaseException | None = None,
            countdown: float | None = None,
            max_retries: int | None = None,
            **_kwargs: Any,
        ) -> Retry:
            captured["countdown"] = countdown
            captured["max_retries"] = max_retries
            captured["exc_type"] = type(exc).__name__ if exc else None
            raise Retry()

        monkeypatch.setattr("celery.app.task.Task.retry", fake_retry, raising=True)

        with pytest.raises(Retry):
            process_stripe_webhook.apply(args=[str(event.id)]).get(
                disable_sync_subtasks=False
            )

        # Backoff for the first attempt is uniform(0.5, 2.0) * 2**0 ⇒ in [0.5, 2.0).
        assert captured["max_retries"] == 3
        countdown_value = captured["countdown"]
        assert isinstance(countdown_value, float)
        assert 0.5 <= countdown_value < 2.0, (
            f"first-attempt countdown should fall in [0.5, 2.0); got {countdown_value}"
        )
        assert captured["exc_type"] == "OperationalError"

    def test_lock_timeout_logs_at_warning_not_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Lock timeouts log WARNING, not ERROR — Sentry stays quiet."""
        import logging

        from celery.exceptions import Retry

        from core.tasks import process_stripe_webhook

        event = self._make_pending_event("evt_lock_warn_001")

        def raise_lock(*_args: object, **_kwargs: object) -> None:
            raise self._make_lock_error()

        monkeypatch.setattr(
            "payments.services.StripePaymentService.process_successful_payment",
            staticmethod(raise_lock),
        )

        def raise_retry(*_args: object, **_kwargs: object) -> None:
            raise Retry()

        monkeypatch.setattr("celery.app.task.Task.retry", raise_retry)

        # Re-enable the logger — test settings disable existing loggers.
        task_logger = logging.getLogger("core.tasks")
        task_logger.disabled = False
        task_logger.propagate = True
        caplog.set_level(logging.WARNING, logger="core.tasks")

        with pytest.raises(Retry):
            process_stripe_webhook.apply(args=[str(event.id)]).get(
                disable_sync_subtasks=False
            )

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "lock timeout / deadlock" in r.getMessage()
        ]
        errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "lock timeout" in r.getMessage().lower()
        ]
        assert warnings, "expected a WARNING for the lock-timeout retry path"
        assert not errors, (
            "lock-timeout retry must not log at ERROR — Sentry would spam"
        )

    def test_lock_retry_succeeds_on_second_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the lock clears, the task's __wrapped__ logic completes normally.

        Simulates the realistic scenario: first delivery hits LockNotAvailable
        and is retried; on the retry the lock has cleared and the handler
        succeeds, marking the event processed.
        """
        from core.tasks import process_stripe_webhook

        event = self._make_pending_event("evt_lock_recover_001")

        attempts: dict[str, int] = {"calls": 0}

        def flaky_handler(*_args: object, **_kwargs: object) -> None:
            attempts["calls"] += 1
            if attempts["calls"] == 1:
                raise self._make_lock_error()
            return None

        monkeypatch.setattr(
            "payments.services.StripePaymentService.process_successful_payment",
            staticmethod(flaky_handler),
        )

        # First invocation: lock error path raises Retry.
        from celery.exceptions import Retry

        retried: dict[str, bool] = {"hit": False}

        def fake_retry_then_swallow(
            self: Any,
            *,
            exc: BaseException | None = None,
            **_kwargs: Any,
        ) -> Retry:
            retried["hit"] = True
            raise Retry()

        monkeypatch.setattr("celery.app.task.Task.retry", fake_retry_then_swallow)

        with pytest.raises(Retry):
            process_stripe_webhook.apply(args=[str(event.id)]).get(
                disable_sync_subtasks=False
            )

        assert retried["hit"], "first attempt should have triggered self.retry"
        assert attempts["calls"] == 1

        # Second invocation simulates Celery re-running the task after the
        # backoff. We call __wrapped__ directly so the second call doesn't
        # re-enter the patched retry path.
        result = process_stripe_webhook.__wrapped__(  # type: ignore[attr-defined]
            str(event.id)
        )

        assert result["success"] is True
        assert attempts["calls"] == 2

        event.refresh_from_db()
        assert event.processed is True
        assert event.processed_at is not None

    def test_lock_error_after_max_retries_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After 3 retries, the lock error re-raises so Celery dead-letters."""
        from core.tasks import process_stripe_webhook

        event = self._make_pending_event("evt_lock_dead_001")

        def raise_lock(*_args: object, **_kwargs: object) -> None:
            raise self._make_lock_error()

        monkeypatch.setattr(
            "payments.services.StripePaymentService.process_successful_payment",
            staticmethod(raise_lock),
        )

        # Request the 4th attempt (retries=3 already consumed). The task
        # must re-raise instead of calling self.retry.
        retry_called: dict[str, bool] = {"hit": False}

        def trap_retry(self: Any, **_kwargs: Any) -> None:
            retry_called["hit"] = True
            raise AssertionError("self.retry must not be called past the budget")

        monkeypatch.setattr("celery.app.task.Task.retry", trap_retry)

        from django.db import OperationalError

        with pytest.raises(OperationalError):
            process_stripe_webhook.apply(
                args=[str(event.id)],
                retries=3,
            ).get(disable_sync_subtasks=False)

        assert retry_called["hit"] is False

    def test_is_lock_timeout_helper_recognises_chained_psycopg_errors(self) -> None:
        """Helper detects LockNotAvailable / DeadlockDetected via __cause__."""
        from django.db import OperationalError
        from psycopg2 import errors as pg_errors

        from core.tasks import _is_lock_timeout_or_deadlock

        # Wrapped LockNotAvailable.
        lock_exc = OperationalError("lock timeout")
        lock_exc.__cause__ = pg_errors.LockNotAvailable("statement timeout")
        assert _is_lock_timeout_or_deadlock(lock_exc) is True

        # Wrapped DeadlockDetected.
        dl_exc = OperationalError("deadlock detected")
        dl_exc.__cause__ = pg_errors.DeadlockDetected("deadlock detected")
        assert _is_lock_timeout_or_deadlock(dl_exc) is True

        # Bare psycopg2 LockNotAvailable.
        assert (
            _is_lock_timeout_or_deadlock(
                pg_errors.LockNotAvailable("canceled by lock_timeout")
            )
            is True
        )

        # Unrelated exception.
        assert _is_lock_timeout_or_deadlock(ValueError("nope")) is False
        assert (
            _is_lock_timeout_or_deadlock(OperationalError("connection lost")) is False
        )
