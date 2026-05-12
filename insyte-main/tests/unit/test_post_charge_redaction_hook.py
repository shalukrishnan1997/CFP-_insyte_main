"""Unit tests for the auth-time and post-charge redaction enqueue hooks.

Four helpers fire redaction Celery tasks on the Stripe authorization /
charge lifecycle:

* ``BatchPaymentService._enqueue_cvv_redaction`` — sync hook on every
  ``PaymentIntent.create()`` return (success *or* failure).
* ``BatchPaymentService._enqueue_deferred_redaction`` — sync hook on
  ``intent_status == "succeeded"``.
* ``core.tasks._enqueue_cvv_redaction_for_donation`` — webhook handler
  on ``payment_intent.payment_failed`` / ``requires_action`` /
  ``succeeded``.
* ``core.tasks._enqueue_deferred_redaction_for_donation`` — webhook
  handler on ``payment_intent.succeeded``.

Each helper must:

1. Enqueue the right task only for placeholders in the matching state.
2. Be a no-op for placeholders past that state (idempotency for
   duplicate webhook deliveries).
3. Be a no-op when the donation has no scan placeholder.
4. Swallow ``.delay`` failures so a queue hiccup never breaks the
   surrounding payment / webhook flow.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.tasks import (
    _enqueue_cvv_redaction_for_donation,
    _enqueue_deferred_redaction_for_donation,
)
from payments.batch_payment import BatchPaymentService
from scans.models import ScanPlaceholder
from tests.factories import DonationFactory, ScanPlaceholderFactory


def _patch_cvv_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Replace ``apply_cvv_redaction_task.delay`` with a recorder."""
    calls: list[tuple[str, ...]] = []

    def fake_delay(*args: str, **_kwargs: Any) -> None:
        calls.append(args)

    from scans import tasks

    monkeypatch.setattr(tasks.apply_cvv_redaction_task, "delay", fake_delay)
    return calls


def _patch_post_charge_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Replace ``apply_deferred_redaction_task.delay`` with a recorder."""
    calls: list[tuple[str, ...]] = []

    def fake_delay(*args: str, **_kwargs: Any) -> None:
        calls.append(args)

    from scans import tasks

    monkeypatch.setattr(tasks.apply_deferred_redaction_task, "delay", fake_delay)
    return calls


@pytest.mark.django_db()
class TestBatchPaymentEnqueueCvvRedaction:
    """Coverage for ``BatchPaymentService._enqueue_cvv_redaction`` (sync auth hook)."""

    def test_fires_when_status_cvv_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auth attempt → CVV task enqueued for CVV_PENDING placeholder."""
        donation = DonationFactory(payment_method="card", payment_status="pending")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        donation.refresh_from_db()
        cvv_calls = _patch_cvv_delay(monkeypatch)
        post_calls = _patch_post_charge_delay(monkeypatch)

        BatchPaymentService._enqueue_cvv_redaction(donation)

        assert cvv_calls == [(str(placeholder.id),)]
        assert post_calls == []

    def test_skips_when_already_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retried auth attempt on a DEFERRED placeholder is a no-op."""
        donation = DonationFactory(payment_method="card", payment_status="failed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
        )
        donation.refresh_from_db()
        cvv_calls = _patch_cvv_delay(monkeypatch)

        BatchPaymentService._enqueue_cvv_redaction(donation)

        assert cvv_calls == []

    def test_skips_when_completed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fully-redacted placeholders are not retriggered."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        donation.refresh_from_db()
        cvv_calls = _patch_cvv_delay(monkeypatch)

        BatchPaymentService._enqueue_cvv_redaction(donation)

        assert cvv_calls == []

    def test_no_placeholder_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Donations without a scanned form must not raise or enqueue."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        cvv_calls = _patch_cvv_delay(monkeypatch)

        BatchPaymentService._enqueue_cvv_redaction(donation)

        assert cvv_calls == []

    def test_delay_failure_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A queue hiccup must not bubble out of the payment flow."""
        donation = DonationFactory(payment_method="card", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        donation.refresh_from_db()

        from scans import tasks

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("broker down")

        monkeypatch.setattr(tasks.apply_cvv_redaction_task, "delay", boom)

        # Must not raise.
        BatchPaymentService._enqueue_cvv_redaction(donation)


@pytest.mark.django_db()
class TestBatchPaymentEnqueuePostChargeRedaction:
    """Coverage for ``BatchPaymentService._enqueue_deferred_redaction`` (sync charge hook)."""

    def test_fires_when_status_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Charge succeeded → post-charge task enqueued for DEFERRED placeholder."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
        )
        donation.refresh_from_db()
        post_calls = _patch_post_charge_delay(monkeypatch)

        BatchPaymentService._enqueue_deferred_redaction(donation)

        assert post_calls == [(str(placeholder.id),)]

    def test_fires_when_status_cvv_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If CVV pass was lost, the post-charge task picks up both layers."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        donation.refresh_from_db()
        post_calls = _patch_post_charge_delay(monkeypatch)

        BatchPaymentService._enqueue_deferred_redaction(donation)

        assert post_calls == [(str(placeholder.id),)]

    def test_skips_when_completed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An already-redacted placeholder must not be enqueued again."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        donation.refresh_from_db()
        post_calls = _patch_post_charge_delay(monkeypatch)

        BatchPaymentService._enqueue_deferred_redaction(donation)

        assert post_calls == []


@pytest.mark.django_db()
class TestWebhookEnqueueRedactionHooks:
    """Coverage for the ``core.tasks`` webhook helpers."""

    def test_cvv_helper_fires_on_cvv_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        donation = DonationFactory(payment_method="card", payment_status="failed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        donation.refresh_from_db()
        calls = _patch_cvv_delay(monkeypatch)

        _enqueue_cvv_redaction_for_donation(donation)

        assert calls == [(str(placeholder.id),)]

    def test_cvv_helper_skips_on_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Webhook re-delivery after sync CVV pass already ran is a no-op."""
        donation = DonationFactory(payment_method="card", payment_status="failed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
        )
        donation.refresh_from_db()
        calls = _patch_cvv_delay(monkeypatch)

        _enqueue_cvv_redaction_for_donation(donation)

        assert calls == []

    def test_post_charge_helper_fires_on_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        donation = DonationFactory(payment_method="card", payment_status="completed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
        )
        donation.refresh_from_db()
        calls = _patch_post_charge_delay(monkeypatch)

        _enqueue_deferred_redaction_for_donation(donation)

        assert calls == [(str(placeholder.id),)]

    def test_post_charge_helper_skips_on_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        donation = DonationFactory(payment_method="card", payment_status="completed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        donation.refresh_from_db()
        calls = _patch_post_charge_delay(monkeypatch)

        _enqueue_deferred_redaction_for_donation(donation)

        assert calls == []
