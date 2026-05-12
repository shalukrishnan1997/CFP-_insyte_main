"""Tests for BatchPaymentService — batch-level Stripe payment processing."""

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import stripe

from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestProcessDonationPayment:
    """Tests for BatchPaymentService.process_donation_payment."""

    def test_returns_error_when_not_card_payment(self) -> None:
        from payments.batch_payment import BatchPaymentService

        donation = DonationFactory(payment_method="direct_debit")
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is False
        assert "not a card payment" in result["error"]

    def test_returns_error_when_not_qa_approved(self) -> None:
        from payments.batch_payment import BatchPaymentService

        donation = DonationFactory(payment_method="card", qa_status="pending")
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is False
        assert "not QA approved" in result["error"]

    def test_skips_when_already_processing(self) -> None:
        from payments.batch_payment import BatchPaymentService

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="processing",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is True
        assert result.get("skipped") is True

    def test_skips_when_already_completed(self) -> None:
        from payments.batch_payment import BatchPaymentService

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="completed",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is True
        assert result.get("skipped") is True

    def test_skips_when_has_active_stripe_payment(self) -> None:
        from payments.batch_payment import BatchPaymentService

        campaign = CampaignFactory()
        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
            campaign=campaign,
        )
        # Create an active Stripe payment
        customer = StripeCustomerFactory(client=campaign.client)
        from tests.factories import StripePaymentFactory

        StripePaymentFactory(
            donation=donation, status="pending", stripe_customer=customer
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is True
        assert result.get("skipped") is True

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    def test_processes_new_donation(
        self, mock_update: MagicMock, mock_process: MagicMock
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.return_value = {"success": True, "payment_id": "pay-123"}
        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_donation_payment(donation, user)

        assert result["success"] is True
        mock_process.assert_called_once()
        assert mock_update.called


@pytest.mark.django_db()
class TestGetCreditCardDonations:
    """Tests for BatchPaymentService.get_credit_card_donations."""

    def test_returns_card_donations_with_pending_or_failed_status(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="failed",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="completed",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="direct_debit",
            payment_status="pending",
        )

        qs = BatchPaymentService.get_credit_card_donations(batch)

        assert qs.count() == 2

    def test_returns_empty_when_no_card_donations(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory()

        qs = BatchPaymentService.get_credit_card_donations(batch)

        assert qs.count() == 0


@pytest.mark.django_db()
class TestGetBatchPaymentSummary:
    """Tests for BatchPaymentService.get_batch_payment_summary."""

    def test_returns_correct_counts_and_amounts(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory(payment_status="pending")
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
            amount=Decimal("100.00"),
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="completed",
            amount=Decimal("50.00"),
        )

        summary = BatchPaymentService.get_batch_payment_summary(batch)

        assert summary["total_donations"] == 2
        assert summary["pending"] == 1
        assert summary["completed"] == 1
        assert summary["total_amount"] == 150.0
        assert summary["payment_status"] == "pending"

    def test_returns_zero_when_no_donations(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory()

        summary = BatchPaymentService.get_batch_payment_summary(batch)

        assert summary["total_donations"] == 0
        assert summary["total_amount"] == 0


@pytest.mark.django_db()
class TestProcessBatchPayments:
    """Tests for BatchPaymentService.process_batch_payments."""

    def test_returns_success_when_no_donations(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory()
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["success"] is True
        assert result["total"] == 0
        assert "No donations to process" in result["message"]

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    @patch("time.sleep")
    def test_processes_all_donations(
        self,
        mock_sleep: MagicMock,
        mock_update: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.return_value = {"success": True, "payment_id": "pay-1"}
        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="failed",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["success"] is True
        assert result["total"] == 2
        assert result["successful"] == 2
        assert result["failed"] == 0
        batch.refresh_from_db()
        assert batch.payment_status == "completed"

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    @patch("time.sleep")
    def test_tracks_failures(
        self,
        mock_sleep: MagicMock,
        mock_update: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.return_value = {"success": False, "error": "Card declined"}
        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["success"] is True
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        batch.refresh_from_db()
        assert batch.payment_status == "failed"

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    @patch("time.sleep")
    def test_calls_progress_callback(
        self,
        mock_sleep: MagicMock,
        mock_update: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.return_value = {"success": True}
        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)
        callback_calls: list[Any] = []
        callback = lambda *args: callback_calls.append(args)  # noqa: E731

        BatchPaymentService.process_batch_payments(
            batch, user, task_progress_callback=callback
        )

        assert len(callback_calls) == 1

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    @patch("time.sleep")
    def test_marks_batch_as_partially_completed(
        self,
        mock_sleep: MagicMock,
        mock_update: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        side_effects = [{"success": True}, {"success": False, "error": "Declined"}]
        mock_process.side_effect = side_effects
        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["successful"] == 1
        assert result["failed"] == 1
        batch.refresh_from_db()
        assert batch.payment_status == "partially_completed"

    @patch("payments.batch_payment.BatchPaymentService._process_single_donation")
    @patch(
        "payments.campaign_payment.CampaignPaymentService.update_campaign_payment_status"
    )
    @patch("time.sleep")
    def test_handles_exception_in_donation_processing(
        self,
        mock_sleep: MagicMock,
        mock_update: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.side_effect = Exception("Unexpected error")
        batch = DonationBatchFactory()
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert "Unexpected error" in result["errors"][0]["error"]


@pytest.mark.django_db()
class TestProcessSingleDonation:
    """Tests for BatchPaymentService._process_single_donation."""

    def test_raises_when_no_donor(self) -> None:
        from payments.batch_payment import BatchPaymentService

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
            donor=None,
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is False

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_returns_success_on_succeeded_payment_intent(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        customer = StripeCustomerFactory()
        mock_customer.return_value = customer
        mock_pi = MagicMock()
        mock_pi.id = "pi_test_123"
        mock_pi.status = "succeeded"
        mock_pi.latest_charge = "ch_test_123"
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is True
        donation.refresh_from_db()
        assert donation.payment_status == "completed"

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_returns_failure_on_requires_payment_method(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        customer = StripeCustomerFactory()
        mock_customer.return_value = customer
        mock_pi = MagicMock()
        mock_pi.id = "pi_test_456"
        mock_pi.status = "requires_payment_method"
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is False
        assert "declined" in result["error"].lower()

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_requires_action_marks_donation_awaiting_authentication(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        """3DS / SCA path persists recovery handles instead of failing."""
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        customer = StripeCustomerFactory(client=donation.campaign.client)
        mock_customer.return_value = customer

        mock_use_stripe_sdk = MagicMock()
        mock_use_stripe_sdk.stripe_js = "https://js.stripe.com/v3/3ds_redirect/abc"
        mock_next_action = MagicMock()
        mock_next_action.use_stripe_sdk = mock_use_stripe_sdk

        mock_pi = MagicMock()
        mock_pi.id = "pi_sca_required_001"
        mock_pi.status = "requires_action"
        mock_pi.next_action = mock_next_action
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        user = UserFactory(is_staff=True)
        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is True
        assert result["awaiting_authentication"] is True
        donation.refresh_from_db()
        assert donation.payment_status == "awaiting_authentication"

        payment = StripePayment.objects.get(donation=donation)
        assert payment.stripe_payment_intent_id == "pi_sca_required_001"
        assert payment.requires_action_url.endswith("3ds_redirect/abc")

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_returns_failure_on_stripe_error(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        mock_customer.side_effect = stripe.StripeError("Card error")

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is False

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_passes_idempotency_key_on_payment_intent_create(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        """An idempotency key tied to donation + attempt is forwarded to Stripe.

        This is what stops a transient network failure between Insyte and
        Stripe from billing the donor twice: if the SDK retries the request,
        Stripe matches the idempotency key and replays the original response
        instead of creating a second charge.
        """
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        customer = StripeCustomerFactory()
        mock_customer.return_value = customer
        mock_pi = MagicMock()
        mock_pi.id = "pi_test_idem_1"
        mock_pi.status = "succeeded"
        mock_pi.latest_charge = "ch_test_idem_1"
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        BatchPaymentService._process_single_donation(donation, user)

        assert mock_pi_create.call_count == 1
        kwargs = mock_pi_create.call_args.kwargs
        assert "idempotency_key" in kwargs, (
            "stripe.PaymentIntent.create must receive idempotency_key to "
            "prevent double-charges on network retries"
        )
        # First attempt for this donation -> attempt_number=1
        assert kwargs["idempotency_key"] == f"don_{donation.id}_attempt_1"

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_idempotency_key_changes_between_attempts(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        """A second manual attempt must get a different idempotency key.

        Otherwise Stripe would replay the first attempt's response forever and
        the donation could never be re-tried after a genuine card decline.
        """
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        customer = StripeCustomerFactory()
        mock_customer.return_value = customer
        mock_pi = MagicMock()
        mock_pi.id = "pi_test_idem_2"
        mock_pi.status = "succeeded"
        mock_pi.latest_charge = "ch_test_idem_2"
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=int(donation.amount * 100),
            currency=donation.currency,
            status=StripePaymentAttempt.STATUS_FAILED,
            attempt_number=1,
            error_message="card declined",
        )

        BatchPaymentService._process_single_donation(donation, user)

        kwargs = mock_pi_create.call_args.kwargs
        assert kwargs["idempotency_key"] == f"don_{donation.id}_attempt_2"


@pytest.mark.django_db()
class TestRetryFailedDonations:
    """Tests for BatchPaymentService.retry_failed_donations."""

    def test_returns_success_when_no_failed_donations(self) -> None:
        from payments.batch_payment import BatchPaymentService

        batch = DonationBatchFactory()
        user = UserFactory(is_staff=True)

        result = BatchPaymentService.retry_failed_donations(batch, user)

        assert result["success"] is True
        assert "No failed donations" in result["message"]

    @patch("payments.batch_payment.BatchPaymentService.process_batch_payments")
    def test_resets_failed_to_pending_and_reprocesses(
        self, mock_process: MagicMock
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_process.return_value = {"success": True, "total": 1}
        batch = DonationBatchFactory()
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="card",
            payment_status="failed",
        )
        user = UserFactory(is_staff=True)

        BatchPaymentService.retry_failed_donations(batch, user)

        donation.refresh_from_db()
        assert donation.payment_status == "pending"
        mock_process.assert_called_once_with(batch, user)
