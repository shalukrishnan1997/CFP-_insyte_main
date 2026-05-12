"""Stripe webhook: auto-approve phone donation when 3DS settles off-call.

When a donor completes SCA via the emailed Stripe Checkout link, the
``payment_intent.succeeded`` webhook arrives later. The donation is
already at ``payment_status=awaiting_authentication`` /
``qa_status=pending``; this hook must promote it to
``qa_status=approved`` if it's still auto-approve eligible — otherwise
the donation stays in the QA queue and a thank-you letter waits on
human action.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.tasks import process_stripe_webhook
from donations.models import Donation
from payments.models import StripeCustomer, StripePayment, StripeWebhookEvent
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    SystemDonorFactory,
    UserFactory,
)


def _seed_event(payment_intent_id: str = "pi_test_123") -> StripeWebhookEvent:
    return StripeWebhookEvent.objects.create(
        stripe_event_id=f"evt_{payment_intent_id}",
        event_type="payment_intent.succeeded",
        payload={"data": {"object": {"id": payment_intent_id, "status": "succeeded"}}},
        processed=False,
        processing_attempts=0,
    )


def _seed_phone_donation_with_payment(
    *,
    intent_id: str = "pi_test_123",
    qa_status: str = Donation.QA_STATUS_PENDING,
    payment_status: str = Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION,
    pending_review_donor: bool = False,
    field_data: dict | None = None,
    payment_method: str = Donation.PAYMENT_METHOD_CARD,
) -> Donation:
    campaign = CampaignFactory()
    batch = DonationBatchFactory(campaign=campaign)
    donor = SystemDonorFactory(
        client=campaign.client, pending_review=pending_review_donor
    )
    operator = UserFactory()
    donation = DonationFactory(
        campaign=campaign,
        batch=batch,
        filled_by=operator,
        donor=None,
        data_file_donor=None,
        system_donor=donor,
        amount=Decimal("25.00"),
        payment_method=payment_method,
        qa_status=qa_status,
        payment_status=payment_status,
        field_data=field_data if field_data is not None else {"intake_method": "phone"},
    )
    stripe_customer = StripeCustomer.objects.create(
        client=campaign.client,
        stripe_customer_id="cus_test",
    )
    StripePayment.objects.create(
        stripe_payment_intent_id=intent_id,
        stripe_customer=stripe_customer,
        donation=donation,
        amount=donation.amount,
        currency=donation.currency,
        status=StripePayment.STATUS_PENDING,
        description="Test",
        processed_by=operator,
    )
    return donation


def _ok_processed_payment(donation: Donation) -> MagicMock:
    """Mock payload returned by ``process_successful_payment``: the function
    flips ``payment_status`` to completed and returns the StripePayment
    instance. We mimic that side-effect for the test."""
    donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
    donation.save(update_fields=["payment_status"])
    payment = donation.stripe_payments.first()
    return payment


@pytest.mark.django_db()
class TestWebhookPhoneAutoApproval:
    """payment_intent.succeeded auto-approves when eligible."""

    @patch("payments.services.StripePaymentService.process_successful_payment")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    def test_eligible_phone_donation_is_auto_approved(
        self,
        _mock_campaign: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        donation = _seed_phone_donation_with_payment()
        event = _seed_event()
        mock_process.side_effect = lambda _intent_id: _ok_processed_payment(donation)

        process_stripe_webhook(event.id)

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert "post-3DS" in donation.qa_notes
        assert "auto_approved_at" in donation.field_data

    @patch("payments.services.StripePaymentService.process_successful_payment")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    def test_non_phone_donation_is_not_auto_approved(
        self,
        _mock_campaign: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        donation = _seed_phone_donation_with_payment(
            field_data={"intake_method": "scan"}
        )
        event = _seed_event()
        mock_process.side_effect = lambda _intent_id: _ok_processed_payment(donation)

        process_stripe_webhook(event.id)

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_PENDING

    @patch("payments.services.StripePaymentService.process_successful_payment")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    def test_pending_review_donor_is_not_auto_approved(
        self,
        _mock_campaign: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        donation = _seed_phone_donation_with_payment(
            pending_review_donor=True,
            qa_status=Donation.QA_STATUS_FLAGGED,
        )
        event = _seed_event()
        mock_process.side_effect = lambda _intent_id: _ok_processed_payment(donation)

        process_stripe_webhook(event.id)

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    @patch("payments.services.StripePaymentService.process_successful_payment")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    def test_already_approved_donation_is_left_alone(
        self,
        _mock_campaign: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        donation = _seed_phone_donation_with_payment(
            qa_status=Donation.QA_STATUS_APPROVED
        )
        event = _seed_event()
        mock_process.side_effect = lambda _intent_id: _ok_processed_payment(donation)
        original_notes = donation.qa_notes

        process_stripe_webhook(event.id)

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        # No double-write — notes unchanged from the original empty value.
        assert donation.qa_notes == original_notes
