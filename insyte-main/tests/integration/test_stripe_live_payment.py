"""Live Stripe (test mode) payment check for ``BatchPaymentService``.

This hits Stripe's API over the network. It is skipped unless
``INSYTE_STRIPE_E2E_SECRET_KEY`` is set to a **test** secret (``sk_test_...``).

Run::

    export INSYTE_STRIPE_E2E_SECRET_KEY=sk_test_...
    uv run pytest tests/integration/test_stripe_live_payment.py -m stripe_e2e --no-cov -v

Optional: ``INSYTE_STRIPE_E2E_PUBLISHABLE_KEY`` (``pk_test_...``) is stored in
``PaymentGatewayConfig`` for parity with production.

Uses Stripe's test fixture PaymentMethod ``pm_card_visa`` (see
https://docs.stripe.com/testing - recommended over raw card numbers in code).
Override with ``INSYTE_STRIPE_E2E_PAYMENT_METHOD_ID`` if needed.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from donations.models import Donation
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PaymentGatewayConfigFactory,
    UserFactory,
)

pytestmark = pytest.mark.stripe_e2e

_SKIP = "INSYTE_STRIPE_E2E_SECRET_KEY not set (use Stripe test secret sk_test_...)"


def _e2e_secret() -> str:
    return os.environ.get("INSYTE_STRIPE_E2E_SECRET_KEY", "").strip()


requires_stripe_e2e = pytest.mark.skipif(not _e2e_secret(), reason=_SKIP)


@pytest.mark.django_db
@requires_stripe_e2e
def test_process_donation_payment_with_test_card_succeeds() -> None:
    """Charge via ``BatchPaymentService`` using Stripe's test Visa fixture PM."""
    secret = _e2e_secret()
    publishable = os.environ.get("INSYTE_STRIPE_E2E_PUBLISHABLE_KEY", "").strip()
    if not publishable:
        publishable = "pk_test_e2e_not_used_for_this_path"

    # Stripe test-mode fixture ID (docs: Testing → PaymentMethods → Visa).
    test_pm_id = os.environ.get(
        "INSYTE_STRIPE_E2E_PAYMENT_METHOD_ID", "pm_card_visa"
    ).strip()

    charity = ClientFactory()
    PaymentGatewayConfigFactory(
        client=charity,
        provider="stripe",
        is_active=True,
        secret_key_encrypted=secret,
        publishable_key_encrypted=publishable,
        webhook_secret_encrypted="",
    )

    campaign = CampaignFactory(client=charity, status="active")
    batch = DonationBatchFactory(campaign=campaign)
    actor = UserFactory()
    donation = DonationFactory(
        campaign=campaign,
        batch=batch,
        amount=Decimal("10.00"),
        currency="GBP",
        payment_method="card",
        qa_status=Donation.QA_STATUS_APPROVED,
        payment_status="pending",
    )

    outcome = BatchPaymentService.process_donation_payment(
        donation,
        actor,
        payment_method_id=test_pm_id,
        require_qa_approved=True,
    )

    assert outcome.get("success") is True, outcome
    donation.refresh_from_db()
    assert donation.payment_status == "completed"

    sp = StripePayment.objects.get(donation=donation)
    assert sp.status == StripePayment.STATUS_SUCCEEDED
    assert sp.stripe_payment_intent_id
