"""End-to-end QA concurrency integration tests.

Covers the row-lock fix that closes the TOCTOU window in the QA approval
flow: two reviewers approving the same card donation simultaneously must
not create two ``StripePayment`` rows.

Concurrency note: pytest-django runs against in-memory SQLite where writers
are serialised at the database level, so the simulated race here issues two
sequential calls that interleave at the same logical point a real race
would hit. The production code path is row-locked via
``SELECT ... FOR UPDATE`` so the same protection holds on Postgres.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from donations.models import Donation
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.fixture()
def _stripe_stub(mocker: MockerFixture) -> MagicMock:
    """Stub Stripe network calls so a successful charge is simulated."""
    mocker.patch(
        "payments.services.StripePaymentService._get_api_key",
        return_value="sk_test_e2e_qa_concurrency",
    )
    mocker.patch(
        "payments.services.StripePaymentService.get_or_create_customer",
        return_value=StripeCustomerFactory(),
    )

    fake_intent = MagicMock()
    fake_intent.id = "pi_e2e_qa_001"
    fake_intent.status = "succeeded"
    fake_intent.latest_charge = "ch_e2e_qa_001"
    fake_intent.to_dict.return_value = {}
    create_mock = mocker.patch("stripe.PaymentIntent.create", return_value=fake_intent)
    return create_mock


@pytest.mark.django_db()
def test_concurrent_approvals_select_for_update_prevents_double_payment(
    _stripe_stub: MagicMock,
) -> None:
    """Two staff users approving the same donation must not double-charge.

    Asserts the canonical contract:
        1. Only one ``StripePayment`` per ``Donation``.
        2. Both calls complete (one succeeds, one no-ops).
        3. No exception leaks.
    """
    batch = DonationBatchFactory(status="pending_qa")
    donation = DonationFactory(
        batch=batch,
        campaign=batch.campaign,
        amount=Decimal("50.00"),
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    reviewer_a = UserFactory(is_staff=True, username="reviewer-a")
    reviewer_b = UserFactory(is_staff=True, username="reviewer-b")

    # Reviewer A approves first — Stripe stub returns succeeded so a
    # StripePayment row is written and donation flips to completed.
    result_a = BatchPaymentService._process_single_donation(donation, reviewer_a)  # pyright: ignore[reportPrivateUsage]

    assert result_a["success"] is True
    assert result_a.get("skipped") is not True
    donation.refresh_from_db()
    assert donation.payment_status == "completed"

    # Mimic reviewer B reading the row before A's transaction commits: only
    # mutate the in-memory copy back to "pending"; the DB row stays at
    # "completed" so the row-lock + re-check inside
    # ``_process_single_donation`` sees the active StripePayment and bails.
    stale_copy_b = Donation.objects.get(pk=donation.pk)
    stale_copy_b.payment_status = "pending"

    result_b = BatchPaymentService._process_single_donation(stale_copy_b, reviewer_b)  # pyright: ignore[reportPrivateUsage]

    # B must not raise and must not write a second charge.
    assert result_b["success"] is True
    assert result_b.get("skipped") is True

    payments = StripePayment.objects.filter(donation=donation)
    assert payments.count() == 1, (
        "Row-lock + re-check inside _process_single_donation must dedupe "
        "two concurrent QA approvals onto a single StripePayment row."
    )


@pytest.mark.django_db()
def test_concurrent_approvals_after_committed_completion_short_circuits(
    _stripe_stub: MagicMock,
) -> None:
    """A second approver arriving after the first commits must no-op.

    This exercises the post-commit path: A finished, donation is
    ``completed``, B fires. The row-lock re-check inside
    ``_process_single_donation`` sees ``payment_status='completed'`` and
    returns ``skipped`` without calling Stripe a second time.
    """
    batch = DonationBatchFactory(status="pending_qa")
    donation = DonationFactory(
        batch=batch,
        campaign=batch.campaign,
        amount=Decimal("75.00"),
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    reviewer_a = UserFactory(is_staff=True, username="reviewer-a-2")
    reviewer_b = UserFactory(is_staff=True, username="reviewer-b-2")

    result_a = BatchPaymentService._process_single_donation(donation, reviewer_a)  # pyright: ignore[reportPrivateUsage]
    assert result_a["success"] is True
    assert _stripe_stub.call_count == 1

    # Refresh state and call B — the row says "completed" so B short-circuits.
    donation.refresh_from_db()
    result_b = BatchPaymentService._process_single_donation(donation, reviewer_b)  # pyright: ignore[reportPrivateUsage]

    assert result_b["success"] is True
    assert result_b.get("skipped") is True
    # Stripe must not have been called a second time.
    assert _stripe_stub.call_count == 1
    assert StripePayment.objects.filter(donation=donation).count() == 1


@pytest.mark.django_db()
def test_concurrent_process_donation_payment_dedupes_via_active_payment_recheck(
    _stripe_stub: MagicMock,
) -> None:
    """The public ``process_donation_payment`` API also dedupes on second call.

    ``process_donation_payment`` checks ``has_active_payment`` before
    delegating to ``_process_single_donation``. After the first call writes
    a SUCCEEDED StripePayment, the second call's pre-check fires and
    short-circuits without entering the lower-level lock — proving the
    public API surface is safe end-to-end.
    """
    batch = DonationBatchFactory(status="pending_qa")
    donation = DonationFactory(
        batch=batch,
        campaign=batch.campaign,
        amount=Decimal("25.00"),
        payment_method="card",
        qa_status="approved",
        payment_status="pending",
    )
    reviewer_a = UserFactory(is_staff=True, username="reviewer-a-3")
    reviewer_b = UserFactory(is_staff=True, username="reviewer-b-3")

    result_a = BatchPaymentService.process_donation_payment(
        donation, reviewer_a, payment_method_id=None, require_qa_approved=False
    )
    assert result_a["success"] is True
    assert _stripe_stub.call_count == 1

    donation.refresh_from_db()
    result_b = BatchPaymentService.process_donation_payment(
        donation, reviewer_b, payment_method_id=None, require_qa_approved=False
    )
    assert result_b["success"] is True
    assert result_b.get("skipped") is True
    assert _stripe_stub.call_count == 1
    assert StripePayment.objects.filter(donation=donation).count() == 1
