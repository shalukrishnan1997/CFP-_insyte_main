"""Tests for the Stripe dispute / chargeback / refund webhook handlers.

Covers ``charge.refunded``, ``charge.dispute.created`` and
``charge.dispute.closed`` event processing in ``StripePaymentService``, plus
their wiring through ``core.tasks.process_stripe_webhook``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from core.tasks import process_stripe_webhook
from payments.models import StripePayment, StripeWebhookEvent
from payments.services import StripePaymentService
from tests.factories import (
    DonationFactory,
    PaymentGatewayConfigFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    UserFactory,
)


def _build_event(event_type: str, event_data: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal Stripe event payload matching the production shape."""
    return {
        "id": f"evt_test_{event_type}",
        "type": event_type,
        "data": {"object": event_data},
    }


def _make_succeeded_payment(charge_id: str = "ch_test_succeeded_001") -> StripePayment:
    """Create a StripePayment in SUCCEEDED state with a linked Donation."""
    customer = StripeCustomerFactory()
    donation = DonationFactory(payment_status="completed")
    payment = StripePaymentFactory(
        stripe_customer=customer,
        donation=donation,
        invoice=None,
        stripe_charge_id=charge_id,
        amount=Decimal("100.00"),
        amount_refunded=Decimal("0.00"),
        status=StripePayment.STATUS_SUCCEEDED,
    )
    return payment


@pytest.mark.django_db()
class TestProcessChargeRefunded:
    """Tests for StripePaymentService.process_charge_refunded."""

    def test_full_refund_marks_payment_and_donation_refunded(self) -> None:
        payment = _make_succeeded_payment()
        UserFactory(is_staff=True, is_active=True)

        event_data = {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,
        }
        result = StripePaymentService.process_charge_refunded(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_REFUNDED
        assert result.amount_refunded == Decimal("100.00")
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        assert payment.donation.payment_status == "refunded"

    def test_partial_refund_marks_payment_partially_refunded(self) -> None:
        payment = _make_succeeded_payment()

        event_data = {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 4000,
        }
        result = StripePaymentService.process_charge_refunded(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_PARTIALLY_REFUNDED
        assert result.amount_refunded == Decimal("40.00")

    def test_creates_staff_notification(self) -> None:
        from notifications.models import Notification

        payment = _make_succeeded_payment()
        staff = UserFactory(is_staff=True, is_active=True)
        UserFactory(is_staff=False, is_active=True)

        event_data = {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,
        }
        StripePaymentService.process_charge_refunded(event_data)

        notes = Notification.objects.filter(
            user=staff, related_object_type="StripePayment"
        )
        assert notes.count() == 1
        assert "refund" in notes.first().title.lower()  # type: ignore[union-attr]

    def test_idempotent_when_already_refunded(self) -> None:
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_REFUNDED
        payment.amount_refunded = Decimal("100.00")
        payment.save(update_fields=["status", "amount_refunded"])
        if payment.donation:
            payment.donation.payment_status = "refunded"
            payment.donation.save(update_fields=["payment_status"])

        event_data = {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,
        }
        result = StripePaymentService.process_charge_refunded(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_REFUNDED

    def test_raises_when_no_matching_payment(self) -> None:
        # Refund webhook arriving before payment_intent.succeeded persists the
        # payment must raise so the Celery task retries instead of silently
        # dropping the refund.
        from payments.services import StripePaymentNotFoundError

        event_data = {
            "id": "ch_unknown",
            "payment_intent": "pi_unknown",
            "amount_refunded": 1000,
        }
        with pytest.raises(StripePaymentNotFoundError):
            StripePaymentService.process_charge_refunded(event_data)

    def test_partial_refund_then_full_refund_promotes_status(self) -> None:
        # A partial refund webhook arrives first, then a follow-up bumps the
        # refund up to the full amount. The payment must promote from
        # PARTIALLY_REFUNDED to REFUNDED — the previous ``>=`` short-circuit
        # would have left it stuck at PARTIALLY_REFUNDED.
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_PARTIALLY_REFUNDED
        payment.amount_refunded = Decimal("50.00")
        payment.save(update_fields=["status", "amount_refunded"])

        event_data = {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,  # full 100.00
        }
        result = StripePaymentService.process_charge_refunded(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_REFUNDED
        assert result.amount_refunded == Decimal("100.00")
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        assert payment.donation.payment_status == "refunded"


@pytest.mark.django_db()
class TestProcessChargeDisputeCreated:
    """Tests for StripePaymentService.process_charge_dispute_created."""

    def test_marks_payment_disputed_and_records_prior_status(self) -> None:
        payment = _make_succeeded_payment()
        UserFactory(is_staff=True, is_active=True)

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount": 10000,
            "currency": "gbp",
            "reason": "fraudulent",
        }
        result = StripePaymentService.process_charge_dispute_created(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_DISPUTED
        assert result.pre_dispute_status == StripePayment.STATUS_SUCCEEDED
        assert result.dispute_reason == "fraudulent"
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        assert payment.donation.payment_status == "disputed"

    def test_creates_staff_notification_with_reason(self) -> None:
        from notifications.models import Notification

        payment = _make_succeeded_payment()
        staff = UserFactory(is_staff=True, is_active=True)

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount": 10000,
            "currency": "gbp",
            "reason": "fraudulent",
        }
        StripePaymentService.process_charge_dispute_created(event_data)

        note = Notification.objects.get(user=staff, related_object_type="StripePayment")
        assert "dispute" in note.title.lower()
        assert "fraudulent" in note.message.lower()

    def test_idempotent_when_already_disputed(self) -> None:
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_DISPUTED
        payment.pre_dispute_status = StripePayment.STATUS_SUCCEEDED
        payment.save(update_fields=["status", "pre_dispute_status"])

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount": 10000,
            "currency": "gbp",
            "reason": "fraudulent",
        }
        result = StripePaymentService.process_charge_dispute_created(event_data)

        assert result is not None
        # pre_dispute_status preserved (not overwritten with the disputed value)
        assert result.pre_dispute_status == StripePayment.STATUS_SUCCEEDED

    def test_dispute_created_before_payment_persisted_raises(self) -> None:
        # Dispute webhook arrives before payment_intent.succeeded persists the
        # payment. Handler must raise so the Celery task retries instead of
        # losing the dispute.
        from payments.services import StripePaymentNotFoundError

        event_data = {
            "charge": "ch_unknown",
            "payment_intent": "pi_unknown",
            "amount": 10000,
            "currency": "gbp",
            "reason": "fraudulent",
        }
        with pytest.raises(StripePaymentNotFoundError):
            StripePaymentService.process_charge_dispute_created(event_data)


@pytest.mark.django_db()
class TestProcessChargeDisputeClosed:
    """Tests for StripePaymentService.process_charge_dispute_closed."""

    def _disputed_payment(self) -> StripePayment:
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_DISPUTED
        payment.pre_dispute_status = StripePayment.STATUS_SUCCEEDED
        payment.save(update_fields=["status", "pre_dispute_status"])
        if payment.donation:
            payment.donation.payment_status = "disputed"
            payment.donation.save(update_fields=["payment_status"])
        return payment

    def test_won_outcome_restores_prior_status(self) -> None:
        payment = self._disputed_payment()
        UserFactory(is_staff=True, is_active=True)

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "status": "won",
        }
        result = StripePaymentService.process_charge_dispute_closed(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_SUCCEEDED
        assert result.pre_dispute_status == ""
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        assert payment.donation.payment_status == "completed"

    def test_lost_outcome_marks_payment_dispute_lost(self) -> None:
        payment = self._disputed_payment()
        UserFactory(is_staff=True, is_active=True)

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "status": "lost",
        }
        result = StripePaymentService.process_charge_dispute_closed(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_DISPUTE_LOST
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        assert payment.donation.payment_status == "dispute_lost"

    def test_warning_outcome_leaves_status_unchanged(self) -> None:
        payment = self._disputed_payment()

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "status": "warning_needs_response",
        }
        result = StripePaymentService.process_charge_dispute_closed(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_DISPUTED

    def test_dispute_closed_won_restores_donation_status_correctly(self) -> None:
        # When the disputed payment was originally PARTIALLY_REFUNDED, a won
        # dispute must restore that prior state on the payment AND map the
        # donation back to its closest non-disputed state ("refunded"). The
        # previous implementation only flipped donation to "completed" when
        # the restored status was SUCCEEDED, leaving it stuck on "disputed".
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_DISPUTED
        payment.pre_dispute_status = StripePayment.STATUS_PARTIALLY_REFUNDED
        payment.amount_refunded = Decimal("40.00")
        payment.save(
            update_fields=[
                "status",
                "pre_dispute_status",
                "amount_refunded",
            ]
        )
        if payment.donation:
            payment.donation.payment_status = "disputed"
            payment.donation.save(update_fields=["payment_status"])
        UserFactory(is_staff=True, is_active=True)

        event_data = {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "status": "won",
        }
        result = StripePaymentService.process_charge_dispute_closed(event_data)

        assert result is not None
        result.refresh_from_db()
        assert result.status == StripePayment.STATUS_PARTIALLY_REFUNDED
        assert result.pre_dispute_status == ""
        assert payment.donation is not None
        payment.donation.refresh_from_db()
        # Partial-refund-restored maps to "refunded" — not stuck on "disputed".
        assert payment.donation.payment_status == "refunded"


@pytest.mark.django_db()
class TestWebhookTaskWiring:
    """End-to-end tests through process_stripe_webhook for the new event types."""

    def _store_event(
        self, stripe_event_id: str, event_type: str, event_data: dict[str, Any]
    ) -> StripeWebhookEvent:
        return StripeWebhookEvent.objects.create(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            payload=_build_event(event_type, event_data),
            processed=False,
        )

    def test_charge_refunded_event_processed_end_to_end(self) -> None:
        payment = _make_succeeded_payment()
        UserFactory(is_staff=True, is_active=True)
        event = self._store_event(
            "evt_charge_refunded_001",
            "charge.refunded",
            {
                "id": payment.stripe_charge_id,
                "payment_intent": payment.stripe_payment_intent_id,
                "amount_refunded": 10000,
            },
        )

        result = process_stripe_webhook.__wrapped__(str(event.id))  # type: ignore[attr-defined]

        assert result["success"] is True
        payment.refresh_from_db()
        assert payment.status == StripePayment.STATUS_REFUNDED
        event.refresh_from_db()
        assert event.processed is True

    def test_charge_dispute_created_event_processed_end_to_end(self) -> None:
        payment = _make_succeeded_payment()
        UserFactory(is_staff=True, is_active=True)
        event = self._store_event(
            "evt_dispute_created_001",
            "charge.dispute.created",
            {
                "charge": payment.stripe_charge_id,
                "payment_intent": payment.stripe_payment_intent_id,
                "amount": 10000,
                "currency": "gbp",
                "reason": "fraudulent",
            },
        )

        result = process_stripe_webhook.__wrapped__(str(event.id))  # type: ignore[attr-defined]

        assert result["success"] is True
        payment.refresh_from_db()
        assert payment.status == StripePayment.STATUS_DISPUTED

    def test_charge_dispute_closed_won_restores_status(self) -> None:
        payment = _make_succeeded_payment()
        payment.status = StripePayment.STATUS_DISPUTED
        payment.pre_dispute_status = StripePayment.STATUS_SUCCEEDED
        payment.save(update_fields=["status", "pre_dispute_status"])
        UserFactory(is_staff=True, is_active=True)

        event = self._store_event(
            "evt_dispute_closed_001",
            "charge.dispute.closed",
            {
                "charge": payment.stripe_charge_id,
                "payment_intent": payment.stripe_payment_intent_id,
                "status": "won",
            },
        )

        result = process_stripe_webhook.__wrapped__(str(event.id))  # type: ignore[attr-defined]

        assert result["success"] is True
        payment.refresh_from_db()
        assert payment.status == StripePayment.STATUS_SUCCEEDED

    def test_orphan_charge_event_triggers_retry(self) -> None:
        # Event references an unknown charge — the handler raises
        # StripePaymentNotFoundError. The task converts that into a Celery
        # retry instead of marking the event processed (which would lose the
        # refund/dispute).
        from unittest.mock import patch

        from celery.exceptions import Retry

        UserFactory(is_staff=True, is_active=True)
        event = self._store_event(
            "evt_orphan_refund_retry_001",
            "charge.refunded",
            {
                "id": "ch_unknown_orphan",
                "payment_intent": "pi_unknown_orphan",
                "amount_refunded": 5000,
            },
        )

        # Eager mode: self.retry() raises Retry which propagates out.
        with (
            patch.object(
                process_stripe_webhook, "retry", side_effect=Retry()
            ) as mock_retry,
            pytest.raises(Retry),
        ):
            process_stripe_webhook.apply(args=[str(event.id)], throw=True).get()

        assert mock_retry.called
        event.refresh_from_db()
        # Still unprocessed so a follow-up retry can succeed once the
        # originating payment is persisted.
        assert event.processed is False
        # processing_error captured the not-found message.
        assert "No StripePayment found" in (event.processing_error or "")

    def test_orphan_charge_event_notifies_staff_after_max_retries(self) -> None:
        # When the retry budget is exhausted, the task creates a staff
        # Notification and leaves the event unprocessed for manual replay.
        from notifications.models import Notification

        staff = UserFactory(is_staff=True, is_active=True)
        event = self._store_event(
            "evt_orphan_refund_exhausted_001",
            "charge.refunded",
            {
                "id": "ch_unknown_orphan",
                "payment_intent": "pi_unknown_orphan",
                "amount_refunded": 5000,
            },
        )

        # Drop max_retries to 0 so the very first attempt skips the retry
        # branch and falls through to the orphan-notification path.
        original_max = process_stripe_webhook.max_retries
        process_stripe_webhook.max_retries = 0
        try:
            result = process_stripe_webhook.apply(
                args=[str(event.id)], throw=True
            ).get()
        finally:
            process_stripe_webhook.max_retries = original_max

        assert result["success"] is False
        event.refresh_from_db()
        assert event.processed is False
        notes = Notification.objects.filter(
            user=staff, related_object_type="StripeWebhookEvent"
        )
        assert notes.count() == 1
        assert "unresolved" in notes.first().title.lower()  # type: ignore[union-attr]

    def test_replay_of_processed_event_short_circuits(self) -> None:
        payment = _make_succeeded_payment()
        UserFactory(is_staff=True, is_active=True)
        event = self._store_event(
            "evt_replay_001",
            "charge.refunded",
            {
                "id": payment.stripe_charge_id,
                "payment_intent": payment.stripe_payment_intent_id,
                "amount_refunded": 10000,
            },
        )

        first = process_stripe_webhook.__wrapped__(str(event.id))  # type: ignore[attr-defined]
        assert first["success"] is True

        # Replay — same event id, already processed → short-circuits without
        # re-running the handler.
        second = process_stripe_webhook.__wrapped__(str(event.id))  # type: ignore[attr-defined]
        assert second["success"] is True
        assert "Already processed" in second["message"]

        payment.refresh_from_db()
        assert payment.amount_refunded == Decimal("100.00")


@pytest.mark.django_db()
class TestRefundPaymentRaceFix:
    """Regression tests for the admin-refund / charge.refunded race fix.

    The admin path (``StripePaymentService.refund_payment``) and the inbound
    ``charge.refunded`` webhook (``process_charge_refunded``) both write to
    ``StripePayment.amount_refunded``. The webhook always row-locked, but the
    admin path used to read-modify-write without a lock — a webhook landing
    between the admin's read and save would be silently overwritten.

    These tests exercise the fix:
      * ``select_for_update()`` in ``refund_payment`` keeps the row stable for
        the duration of the Stripe call + DB write.
      * ``idempotency_key`` is passed to ``stripe.Refund.create`` so a network
        retry can't double-refund.
    """

    def _payment_with_stripe_config(self) -> StripePayment:
        """Build a SUCCEEDED payment whose customer's client has Stripe config.

        ``refund_payment`` resolves the API key from the active
        ``PaymentGatewayConfig`` for the customer's client, so the test must
        seed one.
        """
        customer = StripeCustomerFactory()
        PaymentGatewayConfigFactory(client=customer.client)
        donation = DonationFactory(payment_status="completed")
        return StripePaymentFactory(
            stripe_customer=customer,
            donation=donation,
            invoice=None,
            stripe_charge_id="ch_race_fix_001",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
            status=StripePayment.STATUS_SUCCEEDED,
        )

    def test_refund_payment_passes_idempotency_key_to_stripe(self) -> None:
        """Admin refund must call ``stripe.Refund.create`` with an idempotency key.

        The key is derived from ``(payment.id, amount_cents, reason)`` so a
        transparent retry of the same logical refund hits Stripe's
        idempotency cache and does not produce a second refund.
        """
        from unittest.mock import MagicMock, patch

        payment = self._payment_with_stripe_config()
        fake_refund = MagicMock(id="re_test_idem", status="succeeded")

        with patch(
            "payments.services.stripe.Refund.create", return_value=fake_refund
        ) as mock_create:
            StripePaymentService.refund_payment(
                payment_id=str(payment.id),
                amount=Decimal("40.00"),
                reason="duplicate",
            )

        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        # sha256(amount_cents:reason) prefix-16 form, reconciled with PR #55.
        import hashlib

        reason_hash = hashlib.sha256(b"4000:duplicate").hexdigest()[:16]
        assert kwargs["idempotency_key"] == f"refund_pmt_{payment.id}_{reason_hash}"
        assert kwargs["amount"] == 4000
        assert kwargs["payment_intent"] == payment.stripe_payment_intent_id

    def test_concurrent_webhook_does_not_double_increment_amount_refunded(
        self,
    ) -> None:
        """Admin refund + concurrent ``charge.refunded`` must net one increment.

        Simulates the race by firing ``process_charge_refunded`` from inside
        the mocked ``stripe.Refund.create`` call — i.e. as if the webhook
        landed mid-flight. After both paths complete, ``amount_refunded`` must
        equal the single refund amount, not double it. The fix keeps the
        admin-side row lock held across the Stripe call so the webhook
        either runs entirely before or entirely after the admin path.
        """
        from unittest.mock import MagicMock, patch

        payment = self._payment_with_stripe_config()
        UserFactory(is_staff=True, is_active=True)
        refund_amount = Decimal("100.00")
        refund_cents = int(refund_amount * 100)

        def fire_webhook_during_refund(*_args: Any, **_kwargs: Any) -> MagicMock:
            # Stripe just confirmed the refund — its async webhook for the
            # same refund event arrives now, racing the admin path.
            StripePaymentService.process_charge_refunded(
                {
                    "id": payment.stripe_charge_id,
                    "payment_intent": payment.stripe_payment_intent_id,
                    "amount_refunded": refund_cents,
                }
            )
            return MagicMock(id="re_test_race", status="succeeded")

        with patch(
            "payments.services.stripe.Refund.create",
            side_effect=fire_webhook_during_refund,
        ):
            StripePaymentService.refund_payment(
                payment_id=str(payment.id),
                amount=refund_amount,
                reason="duplicate",
            )

        payment.refresh_from_db()
        # Single refund increment — not 200.00 from a lost-update double-write.
        assert payment.amount_refunded == refund_amount
        assert payment.status == StripePayment.STATUS_REFUNDED

    def test_refund_payment_acquires_row_lock_on_postgres(self) -> None:
        """``refund_payment`` must call ``select_for_update`` on PostgreSQL.

        This is the load-bearing concurrency primitive — without it the admin
        path can read ``amount_refunded`` before the webhook writes and then
        clobber the webhook's update. The handler is gated on
        ``connection.vendor != "sqlite"`` (tests run against in-memory
        SQLite, where ``SELECT FOR UPDATE`` is a no-op anyway), so we patch
        ``django.db.connections["default"]`` to advertise ``vendor="postgresql"``
        and assert ``select_for_update`` is exercised on the manager.
        """
        from unittest.mock import MagicMock, PropertyMock, patch

        from django.db import connections

        from payments.models import StripePayment as _StripePayment

        payment = self._payment_with_stripe_config()
        sentinel_qs = _StripePayment.objects.filter(pk=payment.pk)

        with (
            patch.object(
                type(connections["default"]),
                "vendor",
                new_callable=PropertyMock,
                return_value="postgresql",
            ),
            patch.object(
                type(_StripePayment.objects),
                "select_for_update",
                return_value=sentinel_qs,
            ) as mock_select,
            patch(
                "payments.services.stripe.Refund.create",
                return_value=MagicMock(id="re_lock_test", status="succeeded"),
            ),
        ):
            StripePaymentService.refund_payment(
                payment_id=str(payment.id),
                amount=Decimal("25.00"),
                reason="lock-check",
            )

        mock_select.assert_called()

    def test_partial_admin_refund_with_concurrent_webhook_is_consistent(
        self,
    ) -> None:
        """Partial admin refund + matching webhook must converge on one value.

        An admin issues a partial refund of 40.00 against a 100.00 payment.
        Mid-flight (during the mocked Stripe call), Stripe's webhook reports
        the same refund as a cumulative 40.00. After both paths complete,
        ``amount_refunded`` must be 40.00 — never 80.00 from a double-write.
        """
        from unittest.mock import MagicMock, patch

        payment = self._payment_with_stripe_config()
        UserFactory(is_staff=True, is_active=True)

        admin_refund = Decimal("40.00")
        refund_cents = int(admin_refund * 100)

        def fire_webhook_during_refund(*_args: Any, **_kwargs: Any) -> MagicMock:
            StripePaymentService.process_charge_refunded(
                {
                    "id": payment.stripe_charge_id,
                    "payment_intent": payment.stripe_payment_intent_id,
                    "amount_refunded": refund_cents,
                }
            )
            return MagicMock(id="re_test_race_partial", status="succeeded")

        with patch(
            "payments.services.stripe.Refund.create",
            side_effect=fire_webhook_during_refund,
        ):
            StripePaymentService.refund_payment(
                payment_id=str(payment.id),
                amount=admin_refund,
                reason="partial",
            )

        payment.refresh_from_db()
        assert payment.amount_refunded == admin_refund
        assert payment.status == StripePayment.STATUS_PARTIALLY_REFUNDED
