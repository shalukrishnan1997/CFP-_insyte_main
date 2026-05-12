"""End-to-end integration test for Stripe card decline + 3DS / SCA fallback.

Drives a 4-donor card slice through ``BatchPaymentService.process_batch_payments``
with the full Stripe surface stubbed out, asserting the mixed-outcome contract
that the QA batch flow relies on:

* Two donations succeed cleanly — ``StripePayment.STATUS_SUCCEEDED`` rows are
  written and the donations flip to ``PAYMENT_STATUS_COMPLETED``.
* One donation is rejected by the issuer — ``stripe.CardError`` (``code=
  "card_declined"``) bubbles up from ``stripe.PaymentIntent.create``, the
  donation flips to ``PAYMENT_STATUS_FAILED`` and no ``StripePayment`` row
  is written.
* One donation hits ``requires_action`` (3DS / SCA) — the donation flips to
  ``PAYMENT_STATUS_AWAITING_AUTHENTICATION`` so QA can dispatch a Stripe
  Checkout authentication link via
  ``StripePaymentService.send_authentication_link_for_donation`` (commit
  c00f126). The follow-up call stamps
  ``StripePayment.authentication_link_sent_at`` (added in migration 0004)
  and queues a donor email through Django's locmem backend.

Stripe network calls (``PaymentIntent.create``, ``PaymentMethod.retrieve``,
``checkout.Session.create``, ``PaymentIntent.cancel``) are all patched. No
external services are touched.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core import mail

from donations.models import Donation
from letters.tasks import build_letter_generation_queryset
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from payments.services import StripePaymentService
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PaymentGatewayConfigFactory,
    StripeCustomerFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from campaigns.models import Campaign
    from clients.models import Client
    from core.models import User
    from donations.models import DonationBatch


# Each donation amount in the card slice has a unique amount-in-pence value
# so the per-call dispatcher can route by that key. Keep these in sync with
# the ``card_slice`` fixture below.
_DONOR_OK_ONE_PENCE = 2500
_DONOR_OK_TWO_PENCE = 4000
_DONOR_DECLINED_PENCE = 6000
_DONOR_THREE_DS_PENCE = 8000

_THREE_DS_REDIRECT_URL = "https://js.stripe.com/v3/3ds/redir"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_card_donation(
    *,
    campaign: Campaign,
    batch: DonationBatch,
    amount: Decimal,
    suffix: str,
) -> Donation:
    """Create a QA-approved card donation primed for batch payment processing.

    Args:
        campaign: Campaign owning the donation.
        batch: ``DonationBatch`` the donation lives in.
        amount: Donation amount in GBP.
        suffix: Unique suffix appended to the donor's email so factories don't
            collide on the unique-email constraint.

    Returns:
        Persisted ``Donation`` row in ``payment_status="pending"``.
    """
    return DonationFactory(
        campaign=campaign,
        batch=batch,
        amount=amount,
        currency="GBP",
        payment_method="card",
        qa_status=Donation.QA_STATUS_APPROVED,
        payment_status=Donation.PAYMENT_STATUS_PENDING,
        donor__email=f"donor-{suffix}@example.test",
    )


def _make_succeeded_intent(intent_id: str, amount_cents: int) -> MagicMock:
    """Build a mock Stripe PaymentIntent in the ``succeeded`` terminal state."""
    intent = MagicMock()
    intent.id = intent_id
    intent.status = "succeeded"
    intent.latest_charge = f"ch_{intent_id}"
    intent.amount = amount_cents
    intent.to_dict.return_value = {"id": intent_id, "status": "succeeded"}
    return intent


def _make_requires_action_intent(intent_id: str) -> MagicMock:
    """Build a mock Stripe PaymentIntent in the 3DS / SCA challenge state.

    The ``next_action.use_stripe_sdk`` attribute must expose a ``stripe_js``
    attribute (not a dict key) — the batch service reads it via
    ``getattr(use_stripe_sdk, "stripe_js", "")``.
    """
    intent = MagicMock()
    intent.id = intent_id
    intent.status = "requires_action"
    intent.latest_charge = None
    intent.client_secret = f"{intent_id}_secret"
    use_stripe_sdk = MagicMock(stripe_js=_THREE_DS_REDIRECT_URL)
    intent.next_action = MagicMock(type="use_stripe_sdk", use_stripe_sdk=use_stripe_sdk)
    intent.to_dict.return_value = {"id": intent_id, "status": "requires_action"}
    return intent


def _make_card_declined_error() -> stripe.CardError:
    """Return a ``stripe.CardError`` mirroring an issuer card-decline response."""
    err = stripe.CardError(
        message="Your card was declined.",
        param="card",
        code="card_declined",
    )
    err.decline_code = "generic_decline"
    return err


def _build_intent_dispatcher(*, prefix: str) -> Callable[..., MagicMock]:
    """Return a ``stripe.PaymentIntent.create`` side_effect for the card slice.

    Routes by ``amount`` (each donation has a unique amount in this fixture):
    succeeded for the two OK donors, ``CardError`` for the declined donor,
    ``requires_action`` for the 3DS donor. ``prefix`` namespaces the intent
    ids so each test can keep its inserts disjoint.
    """

    def _dispatch(**kwargs: object) -> MagicMock:
        amount_value = kwargs["amount"]
        if not isinstance(amount_value, int):
            raise AssertionError(f"Unexpected amount type: {type(amount_value)!r}")
        if amount_value == _DONOR_DECLINED_PENCE:
            raise _make_card_declined_error()
        if amount_value == _DONOR_THREE_DS_PENCE:
            return _make_requires_action_intent(f"pi_{prefix}_three_ds")
        if amount_value in (_DONOR_OK_ONE_PENCE, _DONOR_OK_TWO_PENCE):
            return _make_succeeded_intent(
                f"pi_{prefix}_ok_{amount_value}", amount_value
            )
        raise AssertionError(f"Unexpected charge amount: {amount_value}")

    return _dispatch


@pytest.fixture()
def stripe_mocks() -> Iterator[dict[str, MagicMock]]:
    """Patch the full Stripe surface used by the batch payment path.

    Yields a dict with ``intent_create``, ``pm_retrieve``, ``api_key``,
    ``customer``, ``checkout_create``, and ``intent_cancel`` mocks so each
    test can assign per-donation side effects.
    """
    with (
        patch("payments.services.StripePaymentService._get_api_key") as mock_key,
        patch(
            "payments.services.StripePaymentService.get_or_create_customer"
        ) as mock_customer,
        patch("stripe.PaymentMethod.retrieve") as mock_pm_retrieve,
        patch("stripe.PaymentIntent.create") as mock_intent_create,
        patch("stripe.PaymentIntent.cancel") as mock_intent_cancel,
        patch("stripe.checkout.Session.create") as mock_checkout_create,
    ):
        mock_key.return_value = "sk_test_decline_3ds"

        yield {
            "api_key": mock_key,
            "customer": mock_customer,
            "pm_retrieve": mock_pm_retrieve,
            "intent_create": mock_intent_create,
            "intent_cancel": mock_intent_cancel,
            "checkout_create": mock_checkout_create,
        }


@pytest.fixture()
def card_slice() -> tuple[Client, Campaign, DonationBatch, list[Donation], User]:
    """Build a 4-donor card slice over a single QA-approved batch.

    Donations are ordered by ``created_at`` because that is the order
    ``BatchPaymentService.get_credit_card_donations`` will iterate them in.
    """
    charity = ClientFactory()
    PaymentGatewayConfigFactory(client=charity, is_active=True)
    campaign = CampaignFactory(client=charity, status="active")
    batch = DonationBatchFactory(campaign=campaign, status="approved")

    donor_specs = [
        (Decimal("25.00"), "ok-1"),
        (Decimal("40.00"), "ok-2"),
        (Decimal("60.00"), "declined"),
        (Decimal("80.00"), "three-ds"),
    ]
    donations = [
        _build_card_donation(
            campaign=campaign, batch=batch, amount=amount, suffix=suffix
        )
        for amount, suffix in donor_specs
    ]

    actor = UserFactory(is_staff=True, is_superuser=True)
    return charity, campaign, batch, donations, actor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
def test_batch_with_decline_and_3ds_records_mixed_outcomes(
    stripe_mocks: dict[str, MagicMock],
    card_slice: tuple[Client, Campaign, DonationBatch, list[Donation], User],
) -> None:
    """Full 4-donor batch run produces 2 succeeded, 1 failed, 1 awaiting auth.

    Mirrors what happens when an operator runs "Process payments" on a
    QA-approved batch where a customer's bank declines one card and another
    customer's bank requests SCA. The per-donation outcomes must remain
    independent — a single decline must not poison the rest of the batch.
    """
    _, campaign, batch, donations, actor = card_slice
    ok_one, ok_two, declined, three_ds = donations

    stripe_mocks["customer"].return_value = StripeCustomerFactory(
        client=campaign.client
    )
    stripe_mocks["intent_create"].side_effect = _build_intent_dispatcher(prefix="mixed")

    result = BatchPaymentService.process_batch_payments(batch, actor)

    assert result["success"] is True
    assert result["total"] == 4
    # Both success and awaiting-authentication count as "successful" returns
    # from ``_process_single_donation`` (the SCA branch returns success=True
    # with awaiting_authentication=True so the batch isn't poisoned).
    assert result["successful"] == 3
    assert result["failed"] == 1

    for donation in (ok_one, ok_two, declined, three_ds):
        donation.refresh_from_db()

    assert ok_one.payment_status == Donation.PAYMENT_STATUS_COMPLETED
    assert ok_two.payment_status == Donation.PAYMENT_STATUS_COMPLETED
    assert declined.payment_status == Donation.PAYMENT_STATUS_FAILED
    assert three_ds.payment_status == Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION

    succeeded_payments = StripePayment.objects.filter(
        donation__in=[ok_one, ok_two],
        status=StripePayment.STATUS_SUCCEEDED,
    )
    assert succeeded_payments.count() == 2

    assert not StripePayment.objects.filter(donation=declined).exists()

    # 3DS donation has a PENDING StripePayment row recording the
    # next_action URL so QA can dispatch the Checkout fallback link.
    three_ds_payment = StripePayment.objects.get(donation=three_ds)
    assert three_ds_payment.status == StripePayment.STATUS_PENDING
    assert three_ds_payment.stripe_payment_intent_id == "pi_mixed_three_ds"
    assert three_ds_payment.requires_action_url == _THREE_DS_REDIRECT_URL
    assert three_ds_payment.authentication_link_sent_at is None


@pytest.mark.django_db()
def test_completed_donations_are_eligible_for_letter_generation(
    stripe_mocks: dict[str, MagicMock],
    card_slice: tuple[Client, Campaign, DonationBatch, list[Donation], User],
) -> None:
    """Succeeded donations land in the letter-generation queryset.

    The queryset (``build_letter_generation_queryset``) filters on
    ``qa_status`` rather than ``payment_status``, so the failed and
    awaiting-auth donations remain in the candidate pool — staff suppress
    them via the QA badge / payment_status filter on the operator UI.
    This test pins both halves of the current contract: the two succeeded
    donations are eligible, and the failed / awaiting-auth donations land
    in the expected ``payment_status`` so a future tightening can decide
    explicitly whether to filter them out.
    """
    _, campaign, batch, donations, actor = card_slice
    ok_one, ok_two, declined, three_ds = donations

    stripe_mocks["customer"].return_value = StripeCustomerFactory(
        client=campaign.client
    )
    stripe_mocks["intent_create"].side_effect = _build_intent_dispatcher(
        prefix="letter"
    )

    BatchPaymentService.process_batch_payments(batch, actor)

    eligible_ids = set(
        build_letter_generation_queryset(
            campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        ).values_list("id", flat=True)
    )

    assert ok_one.id in eligible_ids
    assert ok_two.id in eligible_ids

    declined.refresh_from_db()
    three_ds.refresh_from_db()
    assert declined.payment_status == Donation.PAYMENT_STATUS_FAILED
    assert three_ds.payment_status == Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION


@pytest.mark.django_db()
def test_send_authentication_link_stamps_timestamp_and_queues_donor_email(
    stripe_mocks: dict[str, MagicMock],
    card_slice: tuple[Client, Campaign, DonationBatch, list[Donation], User],
) -> None:
    """Dispatching the Checkout fallback link records the audit fields.

    After the batch run leaves the 3DS donation in
    ``awaiting_authentication``, QA hits the "Send authentication link"
    action which calls
    ``StripePaymentService.send_authentication_link_for_donation``. That
    must:

    * call ``stripe.checkout.Session.create`` once,
    * stamp ``StripePayment.authentication_link_sent_at`` (migration 0004),
    * persist the new Checkout session id and re-point
      ``stripe_payment_intent_id`` at the Checkout-owned intent,
    * queue exactly one outbound email to the donor.
    """
    _, campaign, batch, donations, actor = card_slice
    three_ds = donations[3]

    stripe_mocks["customer"].return_value = StripeCustomerFactory(
        client=campaign.client
    )
    stripe_mocks["intent_create"].side_effect = _build_intent_dispatcher(prefix="setup")

    BatchPaymentService.process_batch_payments(batch, actor)
    three_ds.refresh_from_db()
    assert three_ds.payment_status == Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION

    fallback_session = MagicMock()
    fallback_session.id = "cs_test_3ds_fallback"
    fallback_session.url = "https://checkout.stripe.com/c/pay/cs_test_3ds_fallback"
    fallback_session.payment_intent = "pi_3ds_fallback_new"
    stripe_mocks["checkout_create"].return_value = fallback_session

    mail.outbox.clear()

    outcome = StripePaymentService.send_authentication_link_for_donation(
        donation=three_ds,
        success_url="https://insyte.test/payments/success",
        cancel_url="https://insyte.test/payments/cancel",
        user=actor,
    )

    assert outcome["session_id"] == fallback_session.id
    assert outcome["checkout_url"] == fallback_session.url
    assert outcome["donor_email"] == three_ds.donor.email

    stripe_mocks["checkout_create"].assert_called_once()
    checkout_kwargs = stripe_mocks["checkout_create"].call_args.kwargs
    assert checkout_kwargs["customer_email"] == three_ds.donor.email
    line_item = checkout_kwargs["line_items"][0]
    assert line_item["price_data"]["unit_amount"] == _DONOR_THREE_DS_PENCE
    assert line_item["price_data"]["currency"] == "gbp"
    assert checkout_kwargs["metadata"]["donation_id"] == str(three_ds.id)
    assert checkout_kwargs["metadata"]["recovery"] == "3ds_sca"

    # Original SCA-stuck PaymentIntent is best-effort cancelled so the
    # donor's bank releases the authorisation hold.
    stripe_mocks["intent_cancel"].assert_called_once()
    assert stripe_mocks["intent_cancel"].call_args.args[0] == "pi_setup_three_ds"

    payment = StripePayment.objects.get(donation=three_ds)
    assert payment.stripe_checkout_session_id == fallback_session.id
    assert payment.stripe_payment_intent_id == fallback_session.payment_intent
    assert payment.authentication_link_sent_at is not None

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert three_ds.donor.email in sent.to
    assert "Action required" in sent.subject
    assert fallback_session.url in sent.body
