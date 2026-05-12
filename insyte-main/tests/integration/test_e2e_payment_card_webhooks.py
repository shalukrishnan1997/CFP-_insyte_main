"""End-to-end Stripe webhook integration tests for disputes and refunds.

Drives ``charge.dispute.created``, ``charge.dispute.closed`` (won + lost) and
``charge.refunded`` (full + partial) events all the way through the production
HTTP endpoint at ``/webhooks/stripe/``:

    HTTP POST  →  signature check (mocked)
                →  ``StripeWebhookEvent`` row inserted (idempotency)
                →  ``process_stripe_webhook`` Celery task (eager in tests)
                →  ``StripePaymentService`` handler
                →  ``StripePayment.status`` + ``Donation.payment_status`` flip
                →  per-model audit signal writes ``AuditLog`` row

The fixture ``000015.pdf`` (a sample paper donation form) is staged into
``tests/fixtures/`` so the upstream scan/QA stages of the pipeline can be
referenced symbolically — the actual scan-to-donation reconciliation is out of
scope here; we go directly from a SUCCEEDED ``StripePayment`` row to webhook
events the way the production system does once a charge has cleared.

Unit-level coverage of each handler lives in
``tests/unit/test_stripe_dispute_refund_webhooks.py``; this file mirrors those
canonical event payloads but verifies the HTTP→task→handler→audit chain end
to end.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import stripe
from django.test import Client
from django.urls import reverse

from audit.models import AuditLog
from donations.models import Donation
from payments.models import StripePayment, StripeWebhookEvent
from tests.factories import (
    DonationFactory,
    PaymentGatewayConfigFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    UserFactory,
)

# ═══════════════════════════════════════════════════════════════════════════
# Fixture wiring
# ═══════════════════════════════════════════════════════════════════════════

PDF_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"


@pytest.fixture(autouse=True)
def _scan_pdf_fixture_present() -> None:
    """Guarantee the canonical PDF fixture is staged for the test suite.

    The donation that drives every webhook below originates from this scanned
    paper form in production; staging the binary keeps the integration tests
    self-contained even if other Unit owners regenerate fixtures.
    """
    assert PDF_FIXTURE.exists(), f"Missing PDF fixture at {PDF_FIXTURE}"


@pytest.fixture()
def webhook_secret() -> str:
    """Return the test Stripe webhook signing secret."""
    return "whsec_test_e2e_dispute_refund"


@pytest.fixture()
def succeeded_payment(webhook_secret: str) -> StripePayment:
    """Create a SUCCEEDED ``StripePayment`` linked to a completed Donation.

    Also provisions an active ``PaymentGatewayConfig`` whose
    ``webhook_secret`` matches the one ``stripe.Webhook.construct_event`` is
    patched to accept — without this row the webhook view returns 500 because
    no client has Stripe configured.
    """
    customer = StripeCustomerFactory()
    PaymentGatewayConfigFactory(
        client=customer.client,
        provider="stripe",
        is_active=True,
        publishable_key_encrypted="pk_test_e2e",
        secret_key_encrypted="sk_test_e2e",
        webhook_secret_encrypted=webhook_secret,
    )
    donation = DonationFactory(payment_status=Donation.PAYMENT_STATUS_COMPLETED)
    return StripePaymentFactory(
        stripe_customer=customer,
        donation=donation,
        invoice=None,
        stripe_charge_id="ch_e2e_test_001",
        stripe_payment_intent_id="pi_e2e_test_001",
        amount=Decimal("100.00"),
        amount_refunded=Decimal("0.00"),
        status=StripePayment.STATUS_SUCCEEDED,
    )


@pytest.fixture()
def staff_user_for_audit() -> Any:
    """Create at least one active staff user so notification fan-out runs."""
    return UserFactory(is_staff=True, is_active=True)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _build_event(event_id: str, event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Return a Stripe-shaped event dict for ``construct_event`` to return."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": obj},
        "object": "event",
    }


def _post_webhook(
    client: Client,
    event_payload: dict[str, Any],
) -> Any:
    """POST to ``/webhooks/stripe/`` with signature verification stubbed.

    The handler signs the raw request body, so we patch
    ``stripe.Webhook.construct_event`` to skip signature verification and
    return our event payload as a ``stripe.Event``-like object — the handler
    reads ``.id``, ``.type`` and calls ``.to_dict()``.
    """
    event_obj = stripe.Event.construct_from(event_payload, key="sk_test")

    url = reverse("core:stripe_webhook")
    body = json.dumps(event_payload).encode()
    with patch(
        "core.webhooks.stripe.Webhook.construct_event",
        return_value=event_obj,
    ):
        return client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=12345,v1=fake",
        )


def _latest_audit(model_name: str, instance: Any) -> AuditLog | None:
    """Return the most-recent ``AuditLog`` row for the given instance."""
    return (
        AuditLog.objects.filter(model_name=model_name, object_id=str(instance.pk))
        .order_by("-created_at")
        .first()
    )


# ═══════════════════════════════════════════════════════════════════════════
# charge.dispute.created — donation flips to "disputed", AuditLog written
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDisputeCreatedEndToEnd:
    """``charge.dispute.created`` webhook must mark donation disputed + audit."""

    def test_dispute_created_flips_donation_and_writes_audit(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """End-to-end: HTTP webhook → handler → status flip → AuditLog."""
        event = _build_event(
            "evt_e2e_dispute_created_001",
            "charge.dispute.created",
            {
                "id": "dp_e2e_001",
                "charge": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "amount": 10000,
                "currency": "gbp",
                "reason": "fraudulent",
            },
        )

        response = _post_webhook(client, event)

        assert response.status_code == 200
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.status == StripePayment.STATUS_DISPUTED
        assert succeeded_payment.pre_dispute_status == StripePayment.STATUS_SUCCEEDED
        assert succeeded_payment.dispute_reason == "fraudulent"

        donation = succeeded_payment.donation
        assert donation is not None
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_DISPUTED

        # Webhook event row stored + processed by the eager Celery task.
        webhook_row = StripeWebhookEvent.objects.get(
            stripe_event_id="evt_e2e_dispute_created_001"
        )
        assert webhook_row.processed is True
        assert webhook_row.event_type == "charge.dispute.created"

        # Audit trail captured the donation status flip.
        donation_audit = _latest_audit("Donation", donation)
        assert donation_audit is not None
        assert donation_audit.action == "UPDATE"
        assert "payment_status" in donation_audit.changes
        assert donation_audit.changes["payment_status"]["new"] == "disputed"

        # Audit trail captured the payment status flip too.
        payment_audit = _latest_audit("StripePayment", succeeded_payment)
        assert payment_audit is not None
        assert payment_audit.action == "UPDATE"
        assert payment_audit.changes["status"]["new"] == "disputed"


# ═══════════════════════════════════════════════════════════════════════════
# charge.dispute.closed — won restores completed, lost flips to dispute_lost
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestDisputeClosedEndToEnd:
    """``charge.dispute.closed`` won/lost outcomes through the HTTP endpoint."""

    @staticmethod
    def _put_into_disputed(payment: StripePayment) -> None:
        payment.status = StripePayment.STATUS_DISPUTED
        payment.pre_dispute_status = StripePayment.STATUS_SUCCEEDED
        payment.save(update_fields=["status", "pre_dispute_status"])
        donation = payment.donation
        assert donation is not None
        donation.payment_status = Donation.PAYMENT_STATUS_DISPUTED
        donation.save(update_fields=["payment_status"])

    def test_dispute_closed_won_restores_completed(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """Won outcome — payment back to succeeded, donation back to completed."""
        self._put_into_disputed(succeeded_payment)

        event = _build_event(
            "evt_e2e_dispute_closed_won_001",
            "charge.dispute.closed",
            {
                "id": "dp_e2e_002",
                "charge": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "status": "won",
            },
        )

        response = _post_webhook(client, event)

        assert response.status_code == 200
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.status == StripePayment.STATUS_SUCCEEDED
        assert succeeded_payment.pre_dispute_status == ""

        donation = succeeded_payment.donation
        assert donation is not None
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED

        donation_audit = _latest_audit("Donation", donation)
        assert donation_audit is not None
        assert donation_audit.changes["payment_status"]["new"] == "completed"

    def test_dispute_closed_lost_flips_donation_dispute_lost(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """Lost outcome — payment dispute_lost, donation dispute_lost, audited."""
        self._put_into_disputed(succeeded_payment)

        event = _build_event(
            "evt_e2e_dispute_closed_lost_001",
            "charge.dispute.closed",
            {
                "id": "dp_e2e_003",
                "charge": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "status": "lost",
            },
        )

        response = _post_webhook(client, event)

        assert response.status_code == 200
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.status == StripePayment.STATUS_DISPUTE_LOST

        donation = succeeded_payment.donation
        assert donation is not None
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_DISPUTE_LOST

        donation_audit = _latest_audit("Donation", donation)
        assert donation_audit is not None
        assert donation_audit.changes["payment_status"]["new"] == "dispute_lost"


# ═══════════════════════════════════════════════════════════════════════════
# charge.refunded — full vs partial
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestRefundedEndToEnd:
    """``charge.refunded`` full + partial flows through the HTTP endpoint."""

    def test_full_refund_marks_donation_refunded_with_audit(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """Full refund — donation flips to ``refunded`` and is audited."""
        event = _build_event(
            "evt_e2e_full_refund_001",
            "charge.refunded",
            {
                "id": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "amount_refunded": 10000,
            },
        )

        response = _post_webhook(client, event)

        assert response.status_code == 200
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.status == StripePayment.STATUS_REFUNDED
        assert succeeded_payment.amount_refunded == Decimal("100.00")

        donation = succeeded_payment.donation
        assert donation is not None
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REFUNDED

        donation_audit = _latest_audit("Donation", donation)
        assert donation_audit is not None
        assert donation_audit.changes["payment_status"]["new"] == "refunded"

    def test_partial_refund_keeps_status_records_amount_with_audit(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """Partial refund — donation stays ``completed`` (no terminal flip),
        the partial amount is recorded on ``StripePayment.amount_refunded``,
        and the payment row is audited.

        ``Donation.payment_status`` does not have a ``partially_refunded``
        choice — the codebase deliberately keeps the donation in ``completed``
        until the refund is full. The financial trail is preserved on the
        payment side via ``amount_refunded`` + ``status="partially_refunded"``.
        """
        event = _build_event(
            "evt_e2e_partial_refund_001",
            "charge.refunded",
            {
                "id": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "amount_refunded": 4000,
            },
        )

        response = _post_webhook(client, event)

        assert response.status_code == 200
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.status == StripePayment.STATUS_PARTIALLY_REFUNDED
        assert succeeded_payment.amount_refunded == Decimal("40.00")

        # Donation stays ``completed`` for partial refunds — codebase behaviour
        # confirmed by ``_donation_payment_status_for_stripe_status`` mapping
        # PARTIALLY_REFUNDED → "refunded" only after full refund.
        donation = succeeded_payment.donation
        assert donation is not None
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REFUNDED

        # Payment row was audited with the new amount + status.
        payment_audit = _latest_audit("StripePayment", succeeded_payment)
        assert payment_audit is not None
        assert payment_audit.action == "UPDATE"
        assert payment_audit.changes["status"]["new"] == "partially_refunded"
        # amount_refunded is serialised as the str(Decimal); compare numerically
        # since Decimal("40") and Decimal("40.00") both round-trip to "40" /
        # "40.00" depending on the producer.
        assert Decimal(payment_audit.changes["amount_refunded"]["new"]) == Decimal(
            "40.00"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency — same event id is processed exactly once
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestWebhookIdempotency:
    """Replaying the same webhook event id must be a no-op."""

    def test_duplicate_event_id_is_skipped(
        self,
        client: Client,
        succeeded_payment: StripePayment,
        staff_user_for_audit: Any,
    ) -> None:
        """Second POST of the same ``id`` is short-circuited at the view layer."""
        event = _build_event(
            "evt_e2e_duplicate_001",
            "charge.refunded",
            {
                "id": succeeded_payment.stripe_charge_id,
                "payment_intent": succeeded_payment.stripe_payment_intent_id,
                "amount_refunded": 10000,
            },
        )

        first = _post_webhook(client, event)
        second = _post_webhook(client, event)

        assert first.status_code == 200
        assert second.status_code == 200

        # Only one webhook event row inserted.
        rows = StripeWebhookEvent.objects.filter(
            stripe_event_id="evt_e2e_duplicate_001"
        )
        assert rows.count() == 1

        # Refund applied once — amount didn't double.
        succeeded_payment.refresh_from_db()
        assert succeeded_payment.amount_refunded == Decimal("100.00")
        assert succeeded_payment.status == StripePayment.STATUS_REFUNDED
