"""End-to-end Stripe card success path with idempotency-key + attempt logging.

Drives a 4-donor card-payment slice through the full pipeline:

    scan ingest (stubbed) -> QA approve -> BatchPaymentService creates
    PaymentIntent -> succeeded -> StripePayment.status=succeeded ->
    Donation.payment_status=completed -> letter eligible.

The Stripe HTTP layer is stubbed via ``unittest.mock.patch`` so the test runs
offline. Real-network Stripe coverage lives in
``tests/integration/test_stripe_live_payment.py`` behind the ``stripe_e2e``
mark.

Key invariants verified:
    * ``stripe.PaymentIntent.create`` is called once per donation.
    * Each call carries a unique ``idempotency_key`` of the form
      ``don_<donation_id>_attempt_1`` (commit ad9d214).
    * Every attempt produces a ``StripePaymentAttempt`` row with
      ``status=succeeded`` and ``attempt_number=1``.
    * Each donation lands in ``payment_status='completed'`` with a matching
      ``StripePayment.status='succeeded'`` row.
    * The campaign's letter-generation queryset includes all four donations
      after payment success.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from core.models import User
from donations.models import Donation, DonationBatch
from letters.tasks import build_letter_generation_queryset
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment, StripePaymentAttempt
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PaymentGatewayConfigFactory,
    UserFactory,
)


@pytest.fixture(scope="module", autouse=True)
def _stage_pdf_fixture() -> None:
    """Stage ``tests/fixtures/000015.pdf`` from the repo root if missing.

    Runs once per module — copying the QA reference PDF on demand keeps
    fresh checkouts working without a manual setup step.
    """
    project_root = Path(__file__).resolve().parents[2]
    target = project_root / "tests" / "fixtures" / "000015.pdf"
    if target.exists():
        return
    source = project_root / "000015.pdf"
    if not source.exists():
        pytest.skip("000015.pdf source PDF not present in repo root")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _make_card_batch(
    *,
    donation_count: int = 4,
    qa_status: str = Donation.QA_STATUS_APPROVED,
) -> tuple[DonationBatch, list[Donation], User]:
    """Build a card-only batch with ``donation_count`` approved donations.

    Args:
        donation_count: Number of card donations to create.
        qa_status: Per-donation QA status. Default mimics the post-QA state
            where each card donation has already been individually approved
            by a reviewer.

    Returns:
        A ``(batch, donations, staff_user)`` tuple ready for batch-payment
        processing. The batch is linked to a fresh client whose
        ``PaymentGatewayConfig`` carries dummy Stripe test keys.
    """
    charity = ClientFactory(name="Card-Path Charity")
    PaymentGatewayConfigFactory(
        client=charity,
        provider="stripe",
        is_active=True,
        publishable_key_encrypted="pk_test_card_path",
        secret_key_encrypted="sk_test_card_path",
        webhook_secret_encrypted="whsec_test_card_path",
    )
    campaign = CampaignFactory(client=charity, status="active")
    batch = DonationBatchFactory(campaign=campaign, status="pending_qa")
    staff = UserFactory(is_staff=True, is_superuser=True)

    donations: list[Donation] = []
    for index in range(donation_count):
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            qa_status=qa_status,
            payment_status="pending",
            amount=Decimal("25.00") + Decimal(index),
            currency="GBP",
        )
        donations.append(donation)
    return batch, donations, staff


def _build_succeeded_intent_factory() -> Any:
    """Return a factory function that builds a fresh succeeded PaymentIntent mock.

    Each call returns an independent ``MagicMock`` configured to look like a
    Stripe ``PaymentIntent`` in ``status='succeeded'`` — required because the
    service inspects ``intent.id``, ``intent.status``, ``intent.latest_charge``
    and calls ``intent.to_dict()`` for the persisted snapshot.
    """
    counter = {"n": 0}

    def _build(**_kwargs: Any) -> Any:
        counter["n"] += 1
        idx = counter["n"]
        intent = MagicMock()
        intent.id = f"pi_test_card_success_{idx}"
        intent.status = "succeeded"
        intent.client_secret = f"cs_test_card_success_{idx}"
        intent.amount = 5000
        intent.currency = "gbp"
        intent.latest_charge = f"ch_test_card_success_{idx}"
        intent.to_dict.return_value = {
            "id": intent.id,
            "status": "succeeded",
            "amount": 5000,
            "currency": "gbp",
            "latest_charge": intent.latest_charge,
        }
        return intent

    return _build


def _stripe_customer_mock() -> MagicMock:
    """Return a single ``stripe.Customer`` mock.

    All donations in the test batch share one client, so
    ``get_or_create_customer`` only hits ``stripe.Customer.create`` once and
    caches the local row for subsequent calls.
    """
    customer = MagicMock()
    customer.id = "cus_test_card_success"
    return customer


@pytest.mark.django_db()
class TestE2EStripeCardSuccessPath:
    """Drive 4 donors through scan->QA->batch-payment->letter eligibility."""

    @patch("stripe.Customer.create")
    @patch("stripe.PaymentIntent.create")
    def test_four_donor_batch_payment_succeeds_end_to_end(
        self,
        mock_pi_create: MagicMock,
        mock_customer_create: MagicMock,
    ) -> None:
        """All four card donations charge successfully and become letter-eligible.

        Mirrors the operator workflow described in the task spec: QA has
        already approved each donation individually (so card donations land
        with ``qa_status='approved'``), the operator hits the batch-payment
        button, and ``BatchPaymentService.process_batch_payments`` charges
        every donation in the batch.
        """
        mock_pi_create.side_effect = _build_succeeded_intent_factory()
        mock_customer_create.return_value = _stripe_customer_mock()

        batch, donations, staff = _make_card_batch(donation_count=4)
        campaign = batch.campaign

        result = BatchPaymentService.process_batch_payments(batch, staff)

        assert result["success"] is True
        assert result["total"] == 4
        assert result["successful"] == 4
        assert result["failed"] == 0
        batch.refresh_from_db()
        assert batch.payment_status == "completed"
        assert batch.successful_payment_count == 4
        assert batch.failed_payment_count == 0

        for donation in donations:
            donation.refresh_from_db()
            assert donation.payment_status == "completed"

            payment = StripePayment.objects.get(donation=donation)
            assert payment.status == StripePayment.STATUS_SUCCEEDED
            assert payment.stripe_payment_intent_id.startswith("pi_test_card_success_")
            assert payment.stripe_charge_id.startswith("ch_test_card_success_")
            assert payment.amount == donation.amount

        attempts = StripePaymentAttempt.objects.filter(donation__in=donations)
        assert attempts.count() == 4
        for attempt in attempts:
            assert attempt.status == StripePaymentAttempt.STATUS_SUCCEEDED
            assert attempt.attempt_number == 1
            assert attempt.stripe_payment_intent_id.startswith("pi_test_card_success_")

        # ``get_credit_card_donations`` does not guarantee creation order, so
        # match each call to its donation via the metadata the service
        # includes alongside the idempotency key.
        assert mock_pi_create.call_count == 4
        expected_keys = {f"don_{d.id}_attempt_1" for d in donations}
        seen_keys: set[str] = set()
        for call in mock_pi_create.call_args_list:
            kwargs = call.kwargs
            metadata = kwargs.get("metadata") or {}
            donation_id = metadata.get("donation_id", "")
            assert kwargs.get("idempotency_key") == f"don_{donation_id}_attempt_1", (
                "idempotency_key must follow don_<id>_attempt_<n> so SDK "
                "transparent retries dedupe at Stripe instead of double-charging"
            )
            seen_keys.add(kwargs["idempotency_key"])
        assert seen_keys == expected_keys

        eligible_ids = set(
            build_letter_generation_queryset(
                campaign=campaign,
                donation_filter="all",
                regenerate_mode=False,
            ).values_list("id", flat=True)
        )
        assert eligible_ids == {d.id for d in donations}

    @patch("stripe.Customer.create")
    @patch("stripe.PaymentIntent.create")
    def test_qa_approve_batch_endpoint_runs_after_card_payments_complete(
        self,
        mock_pi_create: MagicMock,
        mock_customer_create: MagicMock,
    ) -> None:
        """QA-approve-batch endpoint succeeds once card payments are completed.

        ``_batch_approval_blocked_response`` blocks card donations that are
        still pending/flagged with ``payment_status in ('pending','failed')``.
        After ``BatchPaymentService.process_batch_payments`` succeeds for all
        donations they sit in ``payment_status='completed'``, so the operator
        can then submit the QA approve-batch endpoint and get a clean
        redirect to the QA dashboard.
        """
        mock_pi_create.side_effect = _build_succeeded_intent_factory()
        mock_customer_create.return_value = _stripe_customer_mock()

        batch, _donations, staff = _make_card_batch(donation_count=4)
        BatchPaymentService.process_batch_payments(batch, staff)

        admin_group, _ = Group.objects.get_or_create(name="admin")
        staff.groups.add(admin_group)

        client = Client()
        client.force_login(staff)
        response = client.post(
            reverse("custom_admin:qa_approve_batch", kwargs={"batch_id": batch.pk}),
            data={"batch_notes": "Card payments cleared; ready for letters."},
        )

        assert response.status_code == 302
        assert response.url.endswith("/admin/qa/")  # type: ignore[attr-defined]
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        eligible_qs = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter="all",
            regenerate_mode=False,
        )
        assert eligible_qs.count() == 4
