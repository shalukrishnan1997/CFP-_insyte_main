"""Live Stripe (test mode) check for the phone-intake auto-approval flow.

Hits Stripe's API over the network. Skipped unless
``INSYTE_STRIPE_E2E_SECRET_KEY`` is set to a Stripe **test** secret
(``sk_test_...``).

What this verifies, end-to-end, against real Stripe:

* ``moto=True, capture_immediately=True`` → Stripe creates a PaymentIntent
  with automatic capture (no ``capture_method`` kwarg) and the donor's
  test card is charged in a single synchronous round-trip.
* Donation lands at ``payment_status=completed`` after the call.
* The view's post-charge auto-approval flips ``qa_status=approved``
  with the auditable note.
* The legacy MOTO defer-capture path (``capture_immediately=False``)
  still produces ``payment_status=requires_capture`` for pending-review
  donors, matching today's behaviour.

Run::

    export INSYTE_STRIPE_E2E_SECRET_KEY=sk_test_...
    export INSYTE_STRIPE_E2E_PUBLISHABLE_KEY=pk_test_...
    uv run pytest tests/integration/test_stripe_live_phone_intake_auto_approval.py \
        -m stripe_e2e --no-cov -v
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from donations.intake import (
    apply_phone_intake_auto_approval,
    is_donation_auto_approve_eligible,
)
from donations.models import Donation
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PaymentGatewayConfigFactory,
    SystemDonorFactory,
    UserFactory,
)

pytestmark = pytest.mark.stripe_e2e

_SKIP = "INSYTE_STRIPE_E2E_SECRET_KEY not set (use Stripe test secret sk_test_...)"


def _e2e_secret() -> str:
    return os.environ.get("INSYTE_STRIPE_E2E_SECRET_KEY", "").strip()


requires_stripe_e2e = pytest.mark.skipif(not _e2e_secret(), reason=_SKIP)


def _gateway_for_charity():
    secret = _e2e_secret()
    publishable = os.environ.get("INSYTE_STRIPE_E2E_PUBLISHABLE_KEY", "").strip()
    if not publishable:
        publishable = "pk_test_e2e_not_used_for_this_path"

    charity = ClientFactory()
    PaymentGatewayConfigFactory(
        client=charity,
        provider="stripe",
        is_active=True,
        secret_key_encrypted=secret,
        publishable_key_encrypted=publishable,
        webhook_secret_encrypted="",
    )
    return charity


def _test_pm_id() -> str:
    """Stripe test-mode fixture PaymentMethod (Visa, no 3DS)."""
    return os.environ.get("INSYTE_STRIPE_E2E_PAYMENT_METHOD_ID", "pm_card_visa").strip()


@pytest.mark.django_db
@requires_stripe_e2e
def test_phone_intake_capture_immediately_lands_at_completed() -> None:
    """Happy path: clean donor + card → Stripe captures synchronously."""
    charity = _gateway_for_charity()
    campaign = CampaignFactory(client=charity, status="active")
    batch = DonationBatchFactory(
        campaign=campaign,
        batch_name=f"Phone — operator — 2026-05-06 — {campaign.name}",
    )
    donor = SystemDonorFactory(client=charity, pending_review=False)
    operator = UserFactory(is_staff=True)

    donation = DonationFactory(
        campaign=campaign,
        batch=batch,
        donor=None,
        data_file_donor=None,
        system_donor=donor,
        amount=Decimal("12.50"),
        currency="GBP",
        payment_method=Donation.PAYMENT_METHOD_CARD,
        qa_status=Donation.QA_STATUS_PENDING,
        payment_status="pending",
        field_data={"intake_method": "phone"},
        filled_by=operator,
    )

    # Pre-charge eligibility check (matches the production view).
    assert is_donation_auto_approve_eligible(donation) is True

    outcome = BatchPaymentService.process_donation_payment(
        donation,
        operator,
        payment_method_id=_test_pm_id(),
        require_qa_approved=False,
        moto=True,
        capture_immediately=True,
    )
    assert outcome.get("success") is True, outcome

    donation.refresh_from_db()
    assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED, (
        f"Expected completed, got {donation.payment_status}"
    )

    sp = StripePayment.objects.get(donation=donation)
    assert sp.status == StripePayment.STATUS_SUCCEEDED
    assert sp.stripe_payment_intent_id.startswith("pi_")
    assert sp.stripe_charge_id.startswith("ch_")  # automatic capture creates a charge

    # Apply the post-charge auto-approval (the view does this).
    apply_phone_intake_auto_approval(
        donation, note="Auto-approved at phone intake — card captured live with donor"
    )
    donation.refresh_from_db()
    assert donation.qa_status == Donation.QA_STATUS_APPROVED
    assert "Auto-approved at phone intake" in donation.qa_notes
    assert "auto_approved_at" in donation.field_data


@pytest.mark.django_db
@requires_stripe_e2e
def test_phone_intake_deferred_capture_lands_at_requires_capture() -> None:
    """Pending-review donor: capture_method=manual → requires_capture state."""
    charity = _gateway_for_charity()
    campaign = CampaignFactory(client=charity, status="active")
    batch = DonationBatchFactory(
        campaign=campaign,
        batch_name=f"Phone — operator — 2026-05-06 — {campaign.name}",
    )
    donor = SystemDonorFactory(client=charity, pending_review=True)
    operator = UserFactory(is_staff=True)

    donation = DonationFactory(
        campaign=campaign,
        batch=batch,
        donor=None,
        data_file_donor=None,
        system_donor=donor,
        amount=Decimal("12.50"),
        currency="GBP",
        payment_method=Donation.PAYMENT_METHOD_CARD,
        qa_status=Donation.QA_STATUS_FLAGGED,
        payment_status="pending",
        field_data={"intake_method": "phone"},
        filled_by=operator,
    )

    # Eligibility must be False for a pending-review donor.
    assert is_donation_auto_approve_eligible(donation) is False

    outcome = BatchPaymentService.process_donation_payment(
        donation,
        operator,
        payment_method_id=_test_pm_id(),
        require_qa_approved=False,
        moto=True,
        capture_immediately=False,
    )
    assert outcome.get("success") is True
    assert outcome.get("requires_capture") is True

    donation.refresh_from_db()
    assert donation.payment_status == Donation.PAYMENT_STATUS_REQUIRES_CAPTURE
    # qa_status untouched — stays flagged, awaiting QA approval.
    assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    sp = StripePayment.objects.get(donation=donation)
    assert sp.status == StripePayment.STATUS_REQUIRES_CAPTURE
    assert sp.stripe_payment_intent_id.startswith("pi_")
