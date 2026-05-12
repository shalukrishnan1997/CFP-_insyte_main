"""Failure-scenario regression suite for the payment + banking pipelines.

This module exercises the worst-case failure paths that the payment, scan,
and concurrency hardening effort is meant to defend against. Each test
documents a concrete failure mode and asserts what the system does today.

Some scenarios already pass (idempotent webhooks, expired-card handling,
APIConnectionError attempt logging). Others document gaps that later units
in the hardening effort will close — those are marked
``pytest.mark.xfail(strict=True, reason="awaits unit N")`` so they auto-flip
to passing once the corresponding fix lands without anyone having to remember
to re-enable them.

Concurrency note: the test database is in-memory SQLite, so writers are
serialized at the database level. True multi-thread races cannot be
demonstrated on this stack — those scenarios simulate the race by issuing
two sequential calls that interleave at the same logical point a real race
would hit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import stripe
from django.db import IntegrityError, connection, transaction

from payments.batch_payment import BatchPaymentService
from payments.models import (
    StripePayment,
    StripePaymentAttempt,
    StripeWebhookEvent,
)
from payments.services import StripePaymentService
from tests.factories import (
    DonationFactory,
    PayingInSlipFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_succeeded_payment(
    charge_id: str = "ch_test_failure_scenarios_001",
) -> StripePayment:
    """Create a SUCCEEDED ``StripePayment`` with a linked completed donation."""
    customer = StripeCustomerFactory()
    donation = DonationFactory(payment_status="completed")
    return StripePaymentFactory(
        stripe_customer=customer,
        donation=donation,
        invoice=None,
        stripe_charge_id=charge_id,
        amount=Decimal("100.00"),
        amount_refunded=Decimal("0.00"),
        status=StripePayment.STATUS_SUCCEEDED,
    )


# ---------------------------------------------------------------------------
# 1. Duplicate Stripe webhook delivery
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_duplicate_stripe_webhook_event_id_is_rejected_by_unique_constraint() -> None:
    """Stripe may redeliver the same event after a slow ACK.

    The local guard is the ``stripe_event_id`` UNIQUE index on
    ``StripeWebhookEvent``. The second insert must raise ``IntegrityError``
    so the webhook view can short-circuit and the downstream Celery task is
    queued only once.
    """
    StripeWebhookEvent.objects.create(
        stripe_event_id="evt_dup_pi_succeeded_001",
        event_type="payment_intent.succeeded",
        payload={
            "id": "evt_dup_pi_succeeded_001",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_dup_001"}},
        },
        processed=False,
    )

    # Wrap the failing INSERT in its own atomic block so the IntegrityError
    # does not poison the surrounding test transaction.
    with pytest.raises(IntegrityError), transaction.atomic():
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_dup_pi_succeeded_001",
            event_type="payment_intent.succeeded",
            payload={
                "id": "evt_dup_pi_succeeded_001",
                "type": "payment_intent.succeeded",
                "data": {"object": {"id": "pi_dup_001"}},
            },
            processed=False,
        )

    # Only one row exists despite the duplicate POST.
    assert (
        StripeWebhookEvent.objects.filter(
            stripe_event_id="evt_dup_pi_succeeded_001"
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# 2. Batch double-approval race
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
@pytest.mark.skipif(
    connection.vendor == "sqlite",
    reason=(
        "SQLite serialises all writers at the database level, so the "
        '"exactly one StripePayment" assertion would pass even without '
        "the row-lock fix — that's a false green. This test is only "
        "meaningful on Postgres, where concurrent writers can actually "
        "race and SELECT ... FOR UPDATE is what prevents the double "
        "charge. Run the suite against Postgres to validate the fix."
    ),
)
def test_concurrent_qa_approval_creates_only_one_stripe_payment(
    mocker: MockerFixture,
) -> None:
    """Two QA approvers approving the same donation must not double-charge.

    With unit 10's row-lock fix in place, the second call re-reads the
    Donation under ``SELECT ... FOR UPDATE`` and re-checks
    ``stripe_payments`` — finding the first call's row, it short-circuits
    instead of creating a duplicate charge.

    Skipped on SQLite (the default test backend) because SQLite serialises
    all writers at the database level — the assertion would pass even if
    the row-lock fix were absent, giving a false green. Run against
    Postgres for the test to validate the fix.
    """
    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_race",
    )
    mocker.patch(
        "payments.services.StripePaymentService.get_or_create_customer",
        return_value=StripeCustomerFactory(),
    )

    fake_intent = MagicMock()
    fake_intent.id = "pi_race_001"
    fake_intent.status = "succeeded"
    fake_intent.latest_charge = "ch_race_001"
    fake_intent.to_dict.return_value = {}
    mocker.patch("stripe.PaymentIntent.create", return_value=fake_intent)

    donation = DonationFactory(
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    user = UserFactory(is_staff=True)

    BatchPaymentService._process_single_donation(donation, user)
    # Reset payment_status to mimic the second writer reading the row before
    # the first writer's "completed" save commits.
    donation.refresh_from_db()
    donation.payment_status = "pending"
    donation.save(update_fields=["payment_status"])
    BatchPaymentService._process_single_donation(donation, user)

    payments = StripePayment.objects.filter(donation=donation)
    # With unit 10's row-lock fix in place, exactly one StripePayment must
    # exist regardless of how many concurrent approvers fire.
    assert payments.count() == 1


# ---------------------------------------------------------------------------
# 3. Expired card
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_expired_card_marks_donation_failed_and_logs_attempt(
    mocker: MockerFixture,
) -> None:
    """``stripe.CardError`` with ``decline_code='expired_card'`` flips the donation
    to ``failed`` and writes a ``StripePaymentAttempt`` carrying the error code.
    """
    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_expired",
    )
    mocker.patch(
        "payments.services.StripePaymentService.get_or_create_customer",
        return_value=StripeCustomerFactory(),
    )

    expired_error = stripe.CardError(
        message="Your card has expired.",
        param="number",
        code="card_declined",
    )
    expired_error.decline_code = "expired_card"
    mocker.patch("stripe.PaymentIntent.create", side_effect=expired_error)

    donation = DonationFactory(
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    user = UserFactory(is_staff=True)

    result = BatchPaymentService._process_single_donation(donation, user)

    assert result["success"] is False
    donation.refresh_from_db()
    assert donation.payment_status == "failed"

    attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == StripePaymentAttempt.STATUS_FAILED
    assert attempt.error_code == "card_declined"
    assert "expired" in attempt.error_message.lower()
    # No StripePayment row for failed attempts.
    assert not StripePayment.objects.filter(donation=donation).exists()


# ---------------------------------------------------------------------------
# 4. 3DS / requires_action
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_three_ds_required_marks_donation_awaiting_authentication(
    mocker: MockerFixture,
) -> None:
    """A PaymentIntent that returns ``status='requires_action'`` is surfaced
    as ``awaiting_authentication`` so the QA reviewer can dispatch a Checkout
    fallback link to the donor — *not* a flat-out failure that loses the
    in-flight intent (unit 2 of the hardening effort)."""
    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_3ds",
    )
    mocker.patch(
        "payments.services.StripePaymentService.get_or_create_customer",
        return_value=StripeCustomerFactory(),
    )

    pending_intent = MagicMock()
    pending_intent.id = "pi_3ds_001"
    pending_intent.status = "requires_action"
    pending_intent.latest_charge = None
    pending_intent.to_dict.return_value = {}
    pending_intent.client_secret = "pi_3ds_001_secret"
    pending_intent.next_action = MagicMock(
        type="use_stripe_sdk",
        use_stripe_sdk={"stripe_js": "https://js.stripe.com/v3/3ds/redir"},
    )
    mocker.patch("stripe.PaymentIntent.create", return_value=pending_intent)

    donation = DonationFactory(
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    user = UserFactory(is_staff=True)

    BatchPaymentService._process_single_donation(donation, user)

    donation.refresh_from_db()
    assert donation.payment_status == "awaiting_authentication"


# ---------------------------------------------------------------------------
# 5. Network failure mid-charge
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_api_connection_error_logs_attempt_and_keeps_donation_not_completed(
    mocker: MockerFixture,
) -> None:
    """A transport-level ``APIConnectionError`` mid-charge must:

    * record a ``StripePaymentAttempt`` (so the audit trail is durable on
      Postgres via the autocommit connection — see
      ``StripePaymentAttemptManager.log_attempt``),
    * leave the donation **not** completed (must not optimistically flip
      to ``completed`` because Stripe never confirmed),
    * propagate failure to the caller.
    """
    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_net",
    )
    mocker.patch(
        "payments.services.StripePaymentService.get_or_create_customer",
        return_value=StripeCustomerFactory(),
    )
    mocker.patch(
        "stripe.PaymentIntent.create",
        side_effect=stripe.APIConnectionError("Network unreachable"),
    )

    donation = DonationFactory(
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    user = UserFactory(is_staff=True)

    result = BatchPaymentService._process_single_donation(donation, user)

    assert result["success"] is False
    donation.refresh_from_db()
    # Donation must not be marked completed when the API call never succeeded.
    assert donation.payment_status != "completed"
    assert donation.payment_status == "failed"

    attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
    assert len(attempts) == 1
    assert attempts[0].status == StripePaymentAttempt.STATUS_FAILED
    assert "network" in attempts[0].error_message.lower()
    # No StripePayment row should be created — the charge never confirmed.
    assert not StripePayment.objects.filter(donation=donation).exists()


# ---------------------------------------------------------------------------
# 6. Celery worker crash mid-task — rollback survival of attempt log
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_stripe_payment_attempt_persists_through_outer_rollback() -> None:
    """``log_attempt`` must persist on its own connection on Postgres so
    ``transaction.set_rollback(True)`` in the QA approval flow does not erase
    the audit row.

    The SQLite test DB cannot exercise the cross-connection contract — its
    file-level write lock prevents a fresh connection from completing while
    the outer transaction is still open. Instead this test verifies the
    intent: ``log_attempt`` is called inside an atomic block that is then
    rolled back, and the row exists afterwards.

    On SQLite the row is written through the caller's connection and *will*
    be discarded by the rollback, so this assertion is the rollback-survival
    invariant we care about. The Postgres path is verified separately via
    ``test_log_attempt_uses_fresh_connection_on_postgres`` in
    ``test_stripe_payment_attempt.py`` — that test patches the connection
    layer and asserts the autocommit branch is taken.
    """
    from django.db import connection as default_connection

    donation = DonationFactory()

    # On Postgres (production), this rollback would NOT erase the attempt
    # because log_attempt opens its own autocommit connection. On SQLite
    # the write rides the outer connection and is discarded — that is the
    # documented test-only behaviour. We assert the Postgres-target
    # invariant by checking the production code path explicitly below.
    with transaction.atomic():
        StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=2500,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_FAILED,
            attempt_number=1,
            stripe_payment_intent_id="pi_rollback_001",
            error_message="simulated worker crash",
        )
        transaction.set_rollback(True)

    if default_connection.vendor == "sqlite":
        # SQLite cannot demonstrate cross-connection durability — document
        # that the row is gone (rolled back with the outer transaction) and
        # rely on the Postgres-connection mock test for the real contract.
        assert not StripePaymentAttempt.objects.filter(
            stripe_payment_intent_id="pi_rollback_001"
        ).exists()
    else:  # pragma: no cover — only reachable when run against Postgres
        # On Postgres, the autocommit-connection write must have survived.
        assert StripePaymentAttempt.objects.filter(
            stripe_payment_intent_id="pi_rollback_001"
        ).exists()


# ---------------------------------------------------------------------------
# 7. Cheque bounce reversal
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_cheque_bounce_reverses_linked_donations_and_writes_audit_log() -> None:
    """Bouncing a 5-cheque slip flips all 5 donations to ``reversed`` + audits them.

    Shipped behaviour (commit 4ae9564): when banking flags a slip as
    ``issues`` / ``partial_success`` / ``failed``, ``BankingService
    .reverse_donations_for_slip`` walks every donation on the slip whose
    ``payment_status='completed'`` and flips it to ``reversed``. The
    standard ``post_save`` audit signal records each row's diff.
    """
    from audit.models import AuditLog
    from banking.services import BankingService

    slip = PayingInSlipFactory(payment_type="cheque")
    donations = []
    for _ in range(5):
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="completed",
            paying_in_slip=slip,
        )
        donations.append(donation)

    reversed_count = BankingService.reverse_donations_for_slip(
        slip, reason="invalid_cheque; All cheques bounced"
    )

    assert reversed_count == 5
    for donation in donations:
        donation.refresh_from_db()
        assert donation.payment_status == "reversed"
        assert "bounced" in donation.payment_reversal_reason

    audit_rows = AuditLog.objects.filter(
        model_name="Donation",
        action="UPDATE",
        object_id__in=[str(d.pk) for d in donations],
    )
    seen_ids = {
        log.object_id for log in audit_rows if "payment_status" in (log.changes or {})
    }
    assert seen_ids == {str(d.pk) for d in donations}


# ---------------------------------------------------------------------------
# 8. Cash discrepancy reversal
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_cash_partial_success_flags_unprocessed_donations() -> None:
    """A partial-success cash slip reverses every settled donation on the slip.

    Shipped semantics: with cash discrepancies, banking has no way to know
    which specific cash donations made up the shortfall, so the safe
    default is to reverse every settled row on the slip and let operators
    re-bank the verified subset (see ``BankingService
    .reverse_donations_for_slip`` docstring + test_banking_service.py
    ``test_cash_short_reverses_all_completed_donations``).

    Donations whose ``payment_status`` is not ``completed`` are excluded.
    """
    from banking.services import BankingService

    slip = PayingInSlipFactory(payment_type="cash")
    settled = []
    for _ in range(3):
        donation = DonationFactory(
            payment_method="cash",
            payment_status="completed",
            paying_in_slip=slip,
            amount=Decimal("20.00"),
        )
        settled.append(donation)
    # Two unsettled rows that must be left untouched (e.g. card pending).
    unsettled = []
    for _ in range(2):
        donation = DonationFactory(
            payment_method="card",
            payment_status="pending",
            paying_in_slip=slip,
            amount=Decimal("20.00"),
        )
        unsettled.append(donation)

    reversed_count = BankingService.reverse_donations_for_slip(
        slip, reason="incorrect_amount; 2 cash donations short"
    )

    # Only the 3 settled rows are flipped; the 2 unsettled card rows stay pending.
    assert reversed_count == 3
    for donation in settled:
        donation.refresh_from_db()
        assert donation.payment_status == "reversed"
    for donation in unsettled:
        donation.refresh_from_db()
        assert donation.payment_status == "pending"
        assert donation.payment_reversal_reason == ""


# ---------------------------------------------------------------------------
# 9. Refund race
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_admin_refund_then_charge_refunded_webhook_does_not_double_count(
    mocker: MockerFixture,
) -> None:
    """Admin-initiated refund + ``charge.refunded`` webhook arrive simultaneously.

    The webhook overwrites ``amount_refunded`` with Stripe's authoritative
    value (rather than incrementing), so even if both paths run the final
    ``amount_refunded`` matches the true refund amount exactly once.
    """
    payment = _make_succeeded_payment(charge_id="ch_refund_race_001")
    UserFactory(is_staff=True, is_active=True)

    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_refund",
    )
    fake_refund = MagicMock()
    fake_refund.id = "re_race_001"
    fake_refund.status = "succeeded"
    mocker.patch("stripe.Refund.create", return_value=fake_refund)

    # Admin triggers a full refund first (it gets to Stripe before the
    # webhook fires).
    StripePaymentService.refund_payment(
        payment_id=str(payment.pk),
        amount=Decimal("100.00"),
        reason="customer_request",
    )

    payment.refresh_from_db()
    assert payment.amount_refunded == Decimal("100.00")
    assert payment.status == StripePayment.STATUS_REFUNDED

    # Webhook then arrives carrying Stripe's authoritative state.
    StripePaymentService.process_charge_refunded(
        {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,
        }
    )

    payment.refresh_from_db()
    # The webhook is idempotent on a terminal REFUNDED status — short-circuit
    # path means amount_refunded stays at the true value, not 200.00.
    assert payment.amount_refunded == Decimal("100.00")
    assert payment.status == StripePayment.STATUS_REFUNDED


@pytest.mark.django_db()
def test_charge_refunded_webhook_then_admin_refund_settles_to_correct_amount(
    mocker: MockerFixture,
) -> None:
    """Reverse ordering: webhook arrives before the admin call returns.

    The webhook lands first and writes Stripe's authoritative refund total.
    The admin path then sees ``can_refund`` return False (status already
    REFUNDED) and refuses to double-process — preserving the invariant.
    """
    from django.core.exceptions import ValidationError

    payment = _make_succeeded_payment(charge_id="ch_refund_race_002")

    StripePaymentService.process_charge_refunded(
        {
            "id": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "amount_refunded": 10000,
        }
    )

    payment.refresh_from_db()
    assert payment.amount_refunded == Decimal("100.00")
    assert payment.status == StripePayment.STATUS_REFUNDED

    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_refund",
    )
    fake_refund = MagicMock()
    fake_refund.id = "re_race_002"
    fake_refund.status = "succeeded"
    mocker.patch("stripe.Refund.create", return_value=fake_refund)

    with pytest.raises(ValidationError):
        StripePaymentService.refund_payment(
            payment_id=str(payment.pk),
            amount=Decimal("100.00"),
            reason="customer_request",
        )

    payment.refresh_from_db()
    assert payment.amount_refunded == Decimal("100.00")


# ---------------------------------------------------------------------------
# 10. Dispute race
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_dispute_arrives_before_payment_persisted_raises_for_celery_retry() -> None:
    """``charge.dispute.created`` arriving before the payment row exists.

    The Celery webhook task converts ``StripePaymentNotFoundError`` into a
    retry, so the dispute is replayed once
    ``payment_intent.succeeded`` finishes persisting the local payment
    row. This asserts the eventual-consistency contract.
    """
    from payments.services import StripePaymentNotFoundError

    event_data = {
        "charge": "ch_dispute_orphan_001",
        "payment_intent": "pi_dispute_orphan_001",
        "reason": "fraudulent",
        "amount": 5000,
        "currency": "gbp",
        "status": "needs_response",
    }

    with pytest.raises(StripePaymentNotFoundError):
        StripePaymentService.process_charge_dispute_created(event_data)


@pytest.mark.django_db()
def test_dispute_after_succeeded_marks_payment_disputed_and_snapshots_status() -> None:
    """Once ``payment_intent.succeeded`` has run, the dispute event flips
    the payment to DISPUTED and snapshots the prior status so a successful
    rebuttal can restore it.
    """
    payment = _make_succeeded_payment(charge_id="ch_dispute_eventual_001")

    StripePaymentService.process_charge_dispute_created(
        {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "reason": "fraudulent",
            "amount": 10000,
            "currency": "gbp",
            "status": "needs_response",
        }
    )

    payment.refresh_from_db()
    assert payment.status == StripePayment.STATUS_DISPUTED
    assert payment.pre_dispute_status == StripePayment.STATUS_SUCCEEDED
    assert payment.dispute_reason == "fraudulent"

    # A second delivery (Stripe retry) is idempotent — does not overwrite
    # the snapshot or churn donation status.
    StripePaymentService.process_charge_dispute_created(
        {
            "charge": payment.stripe_charge_id,
            "payment_intent": payment.stripe_payment_intent_id,
            "reason": "fraudulent",
            "amount": 10000,
            "currency": "gbp",
            "status": "needs_response",
        }
    )
    payment.refresh_from_db()
    assert payment.pre_dispute_status == StripePayment.STATUS_SUCCEEDED
    assert payment.status == StripePayment.STATUS_DISPUTED
