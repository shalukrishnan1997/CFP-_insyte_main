"""Tests for ``sanitize_stripe_stored_payloads`` management command."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from payments.models import StripeWebhookEvent


@pytest.mark.django_db
class TestSanitizeStripeStoredPayloadsCommand:
    def test_summarizes_fat_webhook_payload(self) -> None:
        fat_inner = {
            "id": "pi_fat",
            "object": "payment_intent",
            "status": "succeeded",
            "amount": 2000,
            "currency": "gbp",
            "charges": {"data": [{"id": "ch_1"}]},
            "payment_method": "pm_card_visa",
            "payment_method_options": {"card": {"request_three_d_secure": "automatic"}},
        }
        ev = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_sanitize_test_001",
            event_type="payment_intent.succeeded",
            payload={
                "id": "evt_sanitize_test_001",
                "type": "payment_intent.succeeded",
                "api_version": "2020-08-27",
                "created": 1234567890,
                "livemode": False,
                "data": {"object": fat_inner},
            },
        )

        out = io.StringIO()
        call_command("sanitize_stripe_stored_payloads", stdout=out)
        ev.refresh_from_db()
        inner = (ev.payload.get("data") or {}).get("object") or {}
        assert inner.get("id") == "pi_fat"
        assert inner.get("amount") == 2000
        assert "charges" not in inner
        assert "payment_method_options" not in inner

    def test_dry_run_does_not_persist(self) -> None:
        payload = {
            "id": "evt_dry_002",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_dry",
                    "object": "payment_intent",
                    "status": "succeeded",
                    "amount": 100,
                    "currency": "gbp",
                    "extra_noise": {"nested": [1, 2, 3]},
                }
            },
        }
        ev = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_dry_002",
            event_type="payment_intent.succeeded",
            payload=payload,
        )
        call_command("sanitize_stripe_stored_payloads", "--dry-run")
        ev.refresh_from_db()
        assert ev.payload == payload

    def test_summarizes_stripe_payment_response(self) -> None:
        from tests.factories import StripePaymentFactory

        payment = StripePaymentFactory(
            stripe_response={
                "id": "pi_pay_cmd",
                "object": "payment_intent",
                "status": "succeeded",
                "amount": 500,
                "currency": "gbp",
                "client_secret": "secret_should_drop",
                "charges": {"object": "list"},
            }
        )
        call_command("sanitize_stripe_stored_payloads", "--payments-only")
        payment.refresh_from_db()
        assert payment.stripe_response.get("id") == "pi_pay_cmd"
        assert payment.stripe_response.get("amount") == 500
        assert "client_secret" not in payment.stripe_response
        assert "charges" not in payment.stripe_response
