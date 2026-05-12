"""RBAC test sweep — Unit 4: Webhook HMAC authentication.

Covers every authentication-failure path on the three HMAC-protected
webhook endpoints:

* ``POST /webhooks/stripe/`` — Stripe-signed payment events
* ``POST /webhooks/scan-upload/`` — scanner workstation progress + complete
* ``GET  /webhooks/scanner/campaigns/`` — read-only scanner campaign list

The existing tests in :mod:`tests.unit.test_webhooks`,
:mod:`tests.unit.test_scan_webhook_robustness`, and
:mod:`tests.integration.test_e2e_webhook_security` already cover the
"tampered signature", "replayed nonce", "replayed timestamp", "skew window",
"rate limit", "campaign/client mismatch", "duplicate batch", and
"scanner-campaigns scoped to client" cases.

This file fills the remaining gaps that the RBAC sweep cares about:

1. **Missing signature header** (not just tampered) on every endpoint.
2. **HTTP-method enforcement** on every endpoint.
3. **CSRF-exempt confirmed** with ``Client(enforce_csrf_checks=True)``.
4. **``ClientPortalMiddleware`` bypass** — anonymous request with valid
   HMAC reaches the view without a 2FA-setup or login redirect.
5. **Tampered signature on ``scanner_campaigns``** — closes the gap left
   by the existing scanner-campaigns tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.cache import cache
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from payments.models import StripeWebhookEvent
from scans.models import ScanUploadProgress
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    PaymentGatewayConfigFactory,
)

_SCAN_SECRET = "scan-rbac-secret"
_STRIPE_SECRET = "whsec_rbac_test"


# ─── Helpers ────────────────────────────────────────────────────────


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Compute the HMAC-SHA256 hex digest used by the scanner webhooks.

    Mirrors :func:`core.webhooks._verify_scan_signature`. The signed bytes
    are ``timestamp.encode() + b"." + payload``.

    Args:
        secret: Shared HMAC secret matching ``settings.SCAN_WEBHOOK_SECRET``.
        payload: Raw request body bytes (or query string for GET endpoints).
        timestamp: ``X-Scan-Timestamp`` header value.

    Returns:
        Hex-encoded HMAC-SHA256 digest.
    """
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


@pytest.fixture()
def _scan_secret(settings: SettingsWrapper) -> None:
    """Configure the scan-webhook secret and clear cache before each test."""
    settings.SCAN_WEBHOOK_SECRET = _SCAN_SECRET
    cache.clear()


@pytest.fixture()
def _stripe_secret() -> None:
    """Provision an active client Stripe config so signature verification runs.

    Without an active ``PaymentGatewayConfig`` row, the webhook short-circuits
    at line 101-103 with HTTP 500 ``"Webhook secret not configured"`` before
    any signature check runs — which would mask the auth-failure paths this
    file is supposed to exercise.
    """
    PaymentGatewayConfigFactory(
        provider="stripe",
        is_active=True,
        webhook_secret_encrypted=_STRIPE_SECRET,
    )


# ═══════════════════════════════════════════════════════════════════
# Stripe webhook auth-failure paths
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripeWebhookAuth:
    """Auth-only contracts for ``POST /webhooks/stripe/``."""

    def test_missing_signature_header_returns_400(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
        _stripe_secret: None,
    ) -> None:
        """No ``Stripe-Signature`` header at all → 400, no DB write, no task.

        The view passes ``sig_header=None`` to ``stripe.Webhook.construct_event``
        which raises ``SignatureVerificationError``. With a single configured
        secret the loop exits with ``event=None`` → 400 ``Invalid signature``.
        """
        monkeypatch.setattr(
            "stripe.Webhook.construct_event",
            lambda payload, sig, secret: (_ for _ in ()).throw(
                stripe.SignatureVerificationError("missing sig", sig)
            ),
        )
        called = {"queued": False}
        monkeypatch.setattr(
            "core.tasks.process_stripe_webhook.delay",
            lambda _id: called.update({"queued": True}),
        )

        response = client.post(
            "/webhooks/stripe/",
            data=b'{"id": "evt_no_sig"}',
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json()["error"] == "Invalid signature"
        assert called["queued"] is False
        assert not StripeWebhookEvent.objects.filter(
            stripe_event_id="evt_no_sig"
        ).exists()

    def test_get_method_returns_405(self, client: Client, _stripe_secret: None) -> None:
        """``@require_POST`` rejects GET with 405 before signature check."""
        response = client.get("/webhooks/stripe/")
        assert response.status_code == 405

    def test_put_method_returns_405(self, client: Client, _stripe_secret: None) -> None:
        """``@require_POST`` rejects PUT with 405 before signature check."""
        response = client.put(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
        )
        assert response.status_code == 405

    def test_delete_method_returns_405(
        self, client: Client, _stripe_secret: None
    ) -> None:
        """``@require_POST`` rejects DELETE with 405 before signature check."""
        response = client.delete("/webhooks/stripe/")
        assert response.status_code == 405

    def test_csrf_exempt_post_with_no_csrf_token_is_processed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _stripe_secret: None,
    ) -> None:
        """A POST with no CSRF cookie/token reaches the signature check.

        Uses ``enforce_csrf_checks=True`` to make the assertion meaningful —
        without ``@csrf_exempt`` Django would return 403 *before* the view
        runs. The expected outcome is the signature-verification path
        (here forced into the failure branch), proving the request reached
        the view body.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        monkeypatch.setattr(
            "stripe.Webhook.construct_event",
            lambda payload, sig, secret: (_ for _ in ()).throw(
                stripe.SignatureVerificationError("bad", sig)
            ),
        )

        response = csrf_client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=anything",
        )

        # 400 from the view's signature path, NOT 403 from CSRF middleware.
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid signature"

    def test_replayed_event_id_returns_idempotent_200(
        self,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
        _stripe_secret: None,
    ) -> None:
        """Pre-existing ``stripe_event_id`` short-circuits to 200 with no enqueue.

        Confirms the existing idempotency contract documented at
        :mod:`core.webhooks` lines 124-131 still holds — same event delivered
        twice must NOT enqueue a second Celery task.
        """
        event_id = "evt_rbac_replay_001"
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
        called = {"queued": 0}
        monkeypatch.setattr(
            "core.tasks.process_stripe_webhook.delay",
            lambda _id: called.update({"queued": called["queued"] + 1}),
        )

        response = client.post(
            "/webhooks/stripe/",
            data=b'{"id": "evt_rbac_replay_001"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=anything",
        )

        assert response.status_code == 200
        assert called["queued"] == 0
        # Exactly one row remains — the original.
        assert StripeWebhookEvent.objects.filter(stripe_event_id=event_id).count() == 1


# ═══════════════════════════════════════════════════════════════════
# Scan-upload webhook auth-failure paths
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestScanUploadWebhookAuth:
    """Auth-only contracts for ``POST /webhooks/scan-upload/``."""

    def _payload(self, campaign_id: str, client_id: str) -> dict[str, Any]:
        """Return a minimal valid scanning-status payload for ``(campaign, client)``."""
        return {
            "campaign_id": str(campaign_id),
            "client_id": str(client_id),
            "total_uploaded": 1,
            "total_expected": 5,
            "status": "scanning",
        }

    def test_missing_signature_header_returns_403(
        self, client: Client, _scan_secret: None
    ) -> None:
        """No ``X-Signature`` header at all → 403, no DB row written.

        Without the header the view's ``request.META.get("HTTP_X_SIGNATURE", "")``
        falls back to ``""`` and the HMAC compare fails before any side effect.
        """
        selected_client = ClientFactory(name="No-Sig Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        payload = json.dumps(self._payload(campaign.id, selected_client.id)).encode()

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Invalid signature"
        # No progress row should have been created.
        assert not ScanUploadProgress.objects.filter(campaign=campaign).exists()

    def test_empty_signature_header_returns_403(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Explicit empty ``X-Signature: `` header → 403, same as missing."""
        selected_client = ClientFactory(name="Empty-Sig Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        payload = json.dumps(self._payload(campaign.id, selected_client.id)).encode()

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE="",
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Invalid signature"

    def test_get_method_returns_405(self, client: Client, _scan_secret: None) -> None:
        """``@require_POST`` rejects GET with 405 before signature check."""
        response = client.get("/webhooks/scan-upload/")
        assert response.status_code == 405

    def test_put_method_returns_405(self, client: Client, _scan_secret: None) -> None:
        """``@require_POST`` rejects PUT with 405 before signature check."""
        response = client.put(
            "/webhooks/scan-upload/",
            data=b"{}",
            content_type="application/json",
        )
        assert response.status_code == 405

    def test_csrf_exempt_post_with_no_csrf_token_is_processed(
        self, _scan_secret: None
    ) -> None:
        """CSRF-strict client still reaches the view (``@csrf_exempt`` confirmed).

        Without ``@csrf_exempt`` an unauthenticated cross-origin POST would
        be rejected with 403 by ``CsrfViewMiddleware`` *before* the view
        body runs. Asserting on the signature-failure branch (403) proves
        the view body executed — the alternative reason for 403 (CSRF
        rejection) returns a different content-type and body shape.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            "/webhooks/scan-upload/",
            data=b"{}",
            content_type="application/json",
            HTTP_X_SIGNATURE="not-a-real-signature",
        )

        # 403 with our JSON error payload, NOT the HTML CSRF error page.
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"] == "Invalid signature"

    def test_anonymous_request_with_valid_hmac_bypasses_client_portal_middleware(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Unauthenticated request with valid HMAC must NOT redirect to login/2FA.

        ``ClientPortalMiddleware.process_request`` short-circuits at line 65
        for anonymous users — confirms webhooks are reachable without an
        authenticated session. Asserts the response is a real 200 JSON,
        not a 302 redirect to ``/auth/login/`` or ``/auth/setup-2fa/``.
        """
        selected_client = ClientFactory(name="Anon Webhook Client")
        campaign = CampaignFactory(client=selected_client, status="active")
        payload = json.dumps(self._payload(campaign.id, selected_client.id)).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature(_SCAN_SECRET, payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        # No Location header — so we definitely didn't get redirected.
        assert "Location" not in response.headers
        # And it's our JSON, not Django's redirect HTML.
        assert response.headers["content-type"].startswith("application/json")


# ═══════════════════════════════════════════════════════════════════
# Scanner-campaigns API auth-failure paths
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestScannerCampaignsAuth:
    """Auth-only contracts for ``GET /webhooks/scanner/campaigns/``."""

    def test_missing_signature_header_returns_403(
        self, client: Client, _scan_secret: None
    ) -> None:
        """No ``X-Signature`` header → 403 before any DB read."""
        selected_client = ClientFactory(name="No-Sig Campaigns Client")

        response = client.get(
            "/webhooks/scanner/campaigns/",
            {"client_id": str(selected_client.id)},
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Invalid signature"

    def test_tampered_signature_returns_403(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Wrong-secret HMAC over the query string → 403, no campaigns leaked."""
        selected_client = ClientFactory(name="Tampered Sig Campaigns Client")
        # Existing active campaign — must not appear in any response body.
        CampaignFactory(client=selected_client, status="active")

        query = f"client_id={selected_client.id}"
        ts_str = str(int(time.time()))
        bad_signature = _scan_signature("wrong-secret", query.encode(), ts_str)

        response = client.get(
            "/webhooks/scanner/campaigns/",
            {"client_id": str(selected_client.id)},
            HTTP_X_SIGNATURE=bad_signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "Invalid signature"
        assert "campaigns" not in body

    def test_post_method_returns_405(self, client: Client, _scan_secret: None) -> None:
        """The view explicitly rejects non-GET methods with 405."""
        # No signature needed — the method check runs first.
        response = client.post("/webhooks/scanner/campaigns/")
        assert response.status_code == 405
        assert response.json()["error"] == "GET only"

    def test_put_method_returns_405(self, client: Client, _scan_secret: None) -> None:
        """PUT also rejected with 405 before signature check."""
        response = client.put(
            "/webhooks/scanner/campaigns/",
            data=b"{}",
            content_type="application/json",
        )
        assert response.status_code == 405

    def test_delete_method_returns_405(
        self, client: Client, _scan_secret: None
    ) -> None:
        """DELETE rejected with 405 before signature check."""
        response = client.delete("/webhooks/scanner/campaigns/")
        assert response.status_code == 405

    def test_valid_signature_returns_only_requested_clients_campaigns(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Signed GET returns active campaigns scoped to ``client_id`` only.

        Reinforces the cross-tenant isolation already covered by
        :class:`tests.unit.test_webhooks.TestScannerWebhookIsolation` — kept
        here so an RBAC reviewer can see the auth happy-path explicitly.
        """
        selected = ClientFactory(name="Selected RBAC Client")
        other = ClientFactory(name="Other RBAC Client")
        own_campaign = CampaignFactory(client=selected, status="active")
        CampaignFactory(client=other, status="active")  # must NOT appear

        query = f"client_id={selected.id}"
        ts_str = str(int(time.time()))
        signature = _scan_signature(_SCAN_SECRET, query.encode(), ts_str)

        response = client.get(
            "/webhooks/scanner/campaigns/",
            {"client_id": str(selected.id)},
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        body = response.json()
        ids = {row["id"] for row in body["campaigns"]}
        assert ids == {str(own_campaign.id)}

    def test_anonymous_request_with_valid_hmac_bypasses_client_portal_middleware(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Unauthenticated request with valid HMAC reaches the view body.

        ``ClientPortalMiddleware`` short-circuits for anonymous users so the
        webhook is reachable without authentication. Asserts the response
        is JSON, not a 302 redirect to login/2FA setup.
        """
        selected = ClientFactory(name="Anon Campaign Client")
        own_campaign = CampaignFactory(client=selected, status="active")
        query = f"client_id={selected.id}"
        ts_str = str(int(time.time()))
        signature = _scan_signature(_SCAN_SECRET, query.encode(), ts_str)

        response = client.get(
            "/webhooks/scanner/campaigns/",
            {"client_id": str(selected.id)},
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 200
        assert "Location" not in response.headers
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert {row["id"] for row in body["campaigns"]} == {str(own_campaign.id)}


# ═══════════════════════════════════════════════════════════════════
# Cross-cutting: sanity that scan-upload does *not* leak across clients
# even when an attacker knows the scan secret (campaign/client mismatch)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestScanUploadCampaignClientMismatch:
    """A valid HMAC is not sufficient to act on another tenant's campaign."""

    @patch("scans.tasks.create_scan_batch_from_r2_task.delay")
    def test_valid_hmac_with_foreign_campaign_returns_403_no_ocr(
        self,
        mock_delay: MagicMock,
        client: Client,
        _scan_secret: None,
    ) -> None:
        """Cross-tenant ``(campaign_id, client_id)`` combo → 403, no OCR enqueue.

        The webhook accepts the signature but rejects the request because
        ``campaign.client_id != declared client_id``. Critically, no
        ``ScanUploadProgress`` row should be created and no Celery task
        should be enqueued — otherwise an attacker holding the scan secret
        could create progress records against any campaign on the system.
        """
        attacker_client = ClientFactory(name="Attacker Client")
        victim_client = ClientFactory(name="Victim Client")
        victim_campaign = CampaignFactory(client=victim_client, status="active")
        mock_delay.return_value = SimpleNamespace(id="should-not-fire")

        payload_dict: dict[str, Any] = {
            "campaign_id": str(victim_campaign.id),
            "client_id": str(attacker_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": "ScanOutput/attacker/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-attacker.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature(_SCAN_SECRET, payload, ts_str)

        response = client.post(
            "/webhooks/scan-upload/",
            data=payload,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_SCAN_TIMESTAMP=ts_str,
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Campaign does not belong to client"
        assert not ScanUploadProgress.objects.filter(campaign=victim_campaign).exists()
        mock_delay.assert_not_called()
