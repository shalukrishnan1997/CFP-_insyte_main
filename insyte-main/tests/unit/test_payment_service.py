"""Tests for StripePaymentService — Stripe payment operations."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.exceptions import ValidationError

from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    PaymentGatewayConfigFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
)


@pytest.mark.django_db()
class TestCanRetryCanRefund:
    """Tests for StripePaymentService.can_retry and can_refund."""

    def test_can_retry_when_failed_and_has_retries(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="failed", retry_count=0, max_retries=3)
        assert StripePaymentService.can_retry(payment) is True

    def test_cannot_retry_when_succeeded(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="succeeded", retry_count=0, max_retries=3)
        assert StripePaymentService.can_retry(payment) is False

    def test_cannot_retry_when_max_retries_reached(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="failed", retry_count=3, max_retries=3)
        assert StripePaymentService.can_retry(payment) is False

    def test_can_refund_when_succeeded_and_not_fully_refunded(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )
        assert StripePaymentService.can_refund(payment) is True

    def test_cannot_refund_when_not_succeeded(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="failed", amount=Decimal("100.00"))
        assert StripePaymentService.can_refund(payment) is False

    def test_cannot_refund_when_fully_refunded(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("100.00"),
        )
        assert StripePaymentService.can_refund(payment) is False


@pytest.mark.django_db()
class TestGetApiKey:
    """Tests for StripePaymentService._get_api_key."""

    def test_raises_when_no_client(self) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        with pytest.raises(StripePaymentError, match="Client context is required"):
            StripePaymentService._get_api_key()

    def test_returns_client_config_key_when_available(self) -> None:
        from payments.services import StripePaymentService

        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            secret_key_encrypted="sk_test_client_key",
        )

        key = StripePaymentService._get_api_key(client)
        assert key == "sk_test_client_key"

    def test_raises_when_no_client_config(self) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        client = ClientFactory()  # No PaymentGatewayConfig

        with pytest.raises(
            StripePaymentError, match="not configured or inactive for this client"
        ):
            StripePaymentService._get_api_key(client)

    def test_raises_when_config_has_no_secret_key(self) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            secret_key_encrypted="",  # No secret_key
        )

        with pytest.raises(StripePaymentError, match="secret key is missing"):
            StripePaymentService._get_api_key(client)


@pytest.mark.django_db()
class TestGetPublishableKey:
    """Tests for StripePaymentService._get_publishable_key."""

    def test_raises_when_no_client(self) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        with pytest.raises(StripePaymentError, match="Client context is required"):
            StripePaymentService._get_publishable_key()

    def test_returns_client_config_publishable_key_when_available(self) -> None:
        from payments.services import StripePaymentService

        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            publishable_key_encrypted="pk_test_client_key",
        )

        key = StripePaymentService._get_publishable_key(client)
        assert key == "pk_test_client_key"

    def test_raises_when_publishable_key_missing(self) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            publishable_key_encrypted="",
        )

        with pytest.raises(StripePaymentError, match="publishable key is missing"):
            StripePaymentService._get_publishable_key(client)


@pytest.mark.django_db()
class TestGetOrCreateCustomer:
    """Tests for StripePaymentService.get_or_create_customer."""

    def test_returns_existing_customer_for_client(self) -> None:
        from payments.services import StripePaymentService

        client = ClientFactory()
        existing = StripeCustomerFactory(client=client)

        result = StripePaymentService.get_or_create_customer(
            email="test@test.com", name="Test User", client=client
        )

        assert result.id == existing.id

    @patch("stripe.Customer.create")
    def test_creates_new_customer_via_stripe(self, mock_create: MagicMock) -> None:
        from payments.services import StripePaymentService

        mock_stripe_customer = MagicMock()
        mock_stripe_customer.id = "cus_test_new_123"
        mock_create.return_value = mock_stripe_customer

        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            secret_key_encrypted="sk_test_key",
        )

        result = StripePaymentService.get_or_create_customer(
            email="new@customer.com",
            name="New Customer",
            client=client,
        )

        assert result.stripe_customer_id == "cus_test_new_123"
        assert result.email == "new@customer.com"

    @patch("stripe.Customer.create")
    def test_raises_payment_error_on_stripe_failure(
        self, mock_create: MagicMock
    ) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        mock_create.side_effect = stripe.StripeError("API error")
        client = ClientFactory()
        PaymentGatewayConfigFactory(
            client=client,
            provider="stripe",
            is_active=True,
            secret_key_encrypted="sk_test_key",
        )

        with pytest.raises(StripePaymentError):
            StripePaymentService.get_or_create_customer(
                email="error@test.com", name="Error User", client=client
            )


@pytest.mark.django_db()
class TestProcessSuccessfulPayment:
    """Tests for StripePaymentService.process_successful_payment."""

    def test_returns_none_when_payment_not_found(self) -> None:
        from payments.services import StripePaymentService

        result = StripePaymentService.process_successful_payment("pi_nonexistent")
        assert result is None

    def test_returns_early_when_already_succeeded(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="succeeded")
        result = StripePaymentService.process_successful_payment(
            payment.stripe_payment_intent_id
        )
        assert result is not None
        assert result.status == "succeeded"

    @patch("stripe.PaymentIntent.retrieve")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_processes_pending_payment(
        self, mock_key: MagicMock, mock_retrieve: MagicMock
    ) -> None:
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.latest_charge = "ch_test_123"
        mock_intent.amount = 10000
        mock_intent.to_dict.return_value = {"status": "succeeded"}
        mock_retrieve.return_value = mock_intent

        payment = StripePaymentFactory(status="pending")

        result = StripePaymentService.process_successful_payment(
            payment.stripe_payment_intent_id
        )

        assert result is not None


@pytest.mark.django_db()
class TestProcessFailedPayment:
    """Tests for StripePaymentService.process_failed_payment."""

    def test_returns_none_when_payment_not_found(self) -> None:
        from payments.services import StripePaymentService

        result = StripePaymentService.process_failed_payment("pi_nonexistent")
        assert result is None

    def test_marks_payment_as_failed(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(
            status="processing", retry_count=0, max_retries=0
        )

        result = StripePaymentService.process_failed_payment(
            payment.stripe_payment_intent_id,
            error_message="Card declined",
            error_code="card_declined",
        )

        assert result is not None
        result.refresh_from_db()
        assert result.status == "failed"
        assert result.error_message == "Card declined"

    def test_schedules_retry_when_eligible(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(
            status="processing", retry_count=0, max_retries=3
        )

        result = StripePaymentService.process_failed_payment(
            payment.stripe_payment_intent_id, error_message="Temporary error"
        )

        assert result is not None
        result.refresh_from_db()
        assert result.next_retry_at is not None

    def test_updates_associated_donation_status(self) -> None:
        from payments.services import StripePaymentService

        campaign = CampaignFactory()
        donation = DonationFactory(campaign=campaign, payment_status="processing")
        customer = StripeCustomerFactory(client=campaign.client)
        payment = StripePaymentFactory(
            stripe_customer=customer,
            donation=donation,
            status="processing",
            retry_count=0,
            max_retries=0,
        )

        StripePaymentService.process_failed_payment(
            payment.stripe_payment_intent_id, error_message="Failed"
        )

        donation.refresh_from_db()
        assert donation.payment_status == "failed"


@pytest.mark.django_db()
class TestRefundPayment:
    """Tests for StripePaymentService.refund_payment."""

    def test_raises_validation_error_when_payment_not_found(self) -> None:
        from payments.services import StripePaymentService

        with pytest.raises(ValidationError, match="Payment not found"):
            StripePaymentService.refund_payment("00000000-0000-0000-0000-000000000000")

    def test_raises_validation_error_when_not_refundable(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="failed", amount=Decimal("100.00"))

        with pytest.raises(ValidationError, match="cannot be refunded"):
            StripePaymentService.refund_payment(str(payment.id))

    @patch("stripe.Refund.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_refund_success_full_amount(
        self, mock_key: MagicMock, mock_refund: MagicMock
    ) -> None:
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_refund_obj = MagicMock()
        mock_refund_obj.id = "re_test_123"
        mock_refund_obj.status = "succeeded"
        mock_refund.return_value = mock_refund_obj

        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )

        result = StripePaymentService.refund_payment(str(payment.id))

        assert result["refund_id"] == "re_test_123"
        payment.refresh_from_db()
        assert payment.status in ("refunded", "partially_refunded")

    @patch("stripe.Refund.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_raises_payment_error_on_stripe_failure(
        self, mock_key: MagicMock, mock_refund: MagicMock
    ) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        mock_key.return_value = "sk_test"
        mock_refund.side_effect = stripe.StripeError("Stripe error")

        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )

        with pytest.raises(StripePaymentError):
            StripePaymentService.refund_payment(str(payment.id))

    @patch("stripe.Refund.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_passes_idempotency_key_on_refund_create(
        self, mock_key: MagicMock, mock_refund: MagicMock
    ) -> None:
        """An idempotency key tied to payment + amount + reason is forwarded.

        This prevents a transient network failure from refunding the donor
        twice when the SDK retries the request: Stripe matches the
        idempotency key and replays the original refund response server-side.
        """
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_refund_obj = MagicMock()
        mock_refund_obj.id = "re_test_idem"
        mock_refund_obj.status = "succeeded"
        mock_refund.return_value = mock_refund_obj

        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )

        StripePaymentService.refund_payment(str(payment.id), reason="duplicate")

        assert mock_refund.call_count == 1
        kwargs = mock_refund.call_args.kwargs
        assert "idempotency_key" in kwargs, (
            "stripe.Refund.create must receive idempotency_key to prevent "
            "double-refunds on network retries"
        )
        assert kwargs["idempotency_key"].startswith(f"refund_pmt_{payment.id}_")

    @patch("stripe.Refund.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_refund_idempotency_key_changes_with_reason(
        self, mock_key: MagicMock, mock_refund: MagicMock
    ) -> None:
        """A different reason produces a different idempotency key.

        Without this, a follow-up refund (e.g. reason changed from
        "duplicate" to "fraud") would be silently deduped against the first
        refund by Stripe and fail to actually move money.
        """
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_refund_obj = MagicMock()
        mock_refund_obj.id = "re_test_idem_2"
        mock_refund_obj.status = "succeeded"
        mock_refund.return_value = mock_refund_obj

        # Refund 1: duplicate reason
        payment_a = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )
        StripePaymentService.refund_payment(
            str(payment_a.id), amount=Decimal("10.00"), reason="duplicate"
        )
        key_duplicate = mock_refund.call_args.kwargs["idempotency_key"]

        # Refund 2: different reason on a fresh payment (so the state machine
        # doesn't block us). Same payment ID structure shouldn't matter — the
        # reason hash component must differ.
        payment_b = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
        )
        StripePaymentService.refund_payment(
            str(payment_b.id), amount=Decimal("10.00"), reason="fraud"
        )
        key_fraud = mock_refund.call_args.kwargs["idempotency_key"]

        # Strip the per-payment prefix; only the reason-derived hash should differ.
        hash_duplicate = key_duplicate.rsplit("_", 1)[-1]
        hash_fraud = key_fraud.rsplit("_", 1)[-1]
        assert hash_duplicate != hash_fraud


@pytest.mark.django_db()
class TestLogStripeResponse:
    """Tests for StripePaymentService.log_stripe_response."""

    def test_saves_response_to_payment(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory()
        response = {"id": "pi_test", "status": "succeeded"}

        StripePaymentService.log_stripe_response(payment, response)

        payment.refresh_from_db()
        assert payment.stripe_response == response


@pytest.mark.django_db()
class TestRetryFailedPayment:
    """Tests for StripePaymentService.retry_failed_payment."""

    def test_raises_validation_error_when_not_found(self) -> None:
        from payments.services import StripePaymentService

        with pytest.raises(ValidationError, match="Payment not found"):
            StripePaymentService.retry_failed_payment(
                "00000000-0000-0000-0000-000000000000"
            )

    def test_raises_validation_error_when_cannot_retry(self) -> None:
        from payments.services import StripePaymentService

        payment = StripePaymentFactory(status="succeeded")

        with pytest.raises(ValidationError):
            StripePaymentService.retry_failed_payment(str(payment.id))

    @patch("stripe.PaymentIntent.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_retries_with_new_payment_intent(
        self,
        mock_key: MagicMock,
        mock_pi_create: MagicMock,
        mock_pi_retrieve: MagicMock,
    ) -> None:
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_original_pi = MagicMock()
        mock_original_pi.amount = 10000
        mock_original_pi.currency = "gbp"
        mock_original_pi.customer = "cus_test"
        mock_original_pi.description = "Test donation"
        mock_original_pi.metadata = {}
        mock_pi_retrieve.return_value = mock_original_pi

        mock_new_pi = MagicMock()
        mock_new_pi.id = "pi_retry_123"
        mock_new_pi.status = "processing"
        mock_new_pi.latest_charge = None
        mock_new_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_new_pi

        payment = StripePaymentFactory(status="failed", retry_count=0, max_retries=3)

        result = StripePaymentService.retry_failed_payment(str(payment.id))

        assert result["payment_id"] == str(payment.id)
        assert "retry" in result["status"]

    @patch("stripe.PaymentIntent.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_retry_passes_idempotency_key_on_payment_intent_create(
        self,
        mock_key: MagicMock,
        mock_pi_create: MagicMock,
        mock_pi_retrieve: MagicMock,
    ) -> None:
        """Retry path also forwards an idempotency key keyed on retry_count.

        Network retry of the SAME retry attempt must dedupe; a follow-up
        manual retry (retry_count incremented) must NOT — otherwise the
        donor could never be re-charged after a transient failure.
        """
        from payments.services import StripePaymentService

        mock_key.return_value = "sk_test"
        mock_original_pi = MagicMock()
        mock_original_pi.amount = 10000
        mock_original_pi.currency = "gbp"
        mock_original_pi.customer = "cus_test"
        mock_original_pi.description = "Test donation"
        mock_original_pi.metadata = {}
        mock_pi_retrieve.return_value = mock_original_pi

        mock_new_pi = MagicMock()
        mock_new_pi.id = "pi_retry_idem"
        mock_new_pi.status = "processing"
        mock_new_pi.latest_charge = None
        mock_new_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_new_pi

        payment = StripePaymentFactory(status="failed", retry_count=0, max_retries=3)

        StripePaymentService.retry_failed_payment(str(payment.id))

        assert mock_pi_create.call_count == 1
        kwargs = mock_pi_create.call_args.kwargs
        assert "idempotency_key" in kwargs, (
            "retry path must pass idempotency_key to stripe.PaymentIntent.create"
        )
        # retry_count was 0 when the PI was created, before being incremented.
        assert kwargs["idempotency_key"] == f"pmt_{payment.id}_retry_0"


@pytest.mark.django_db()
class TestSendAuthenticationLinkForDonation:
    """Tests for StripePaymentService.send_authentication_link_for_donation."""

    def test_raises_when_donation_not_awaiting_authentication(self) -> None:
        from payments.services import StripePaymentService

        donation = DonationFactory(payment_method="card", payment_status="pending")
        with pytest.raises(ValidationError, match="not awaiting authentication"):
            StripePaymentService.send_authentication_link_for_donation(
                donation,
                success_url="https://example.test/ok",
                cancel_url="https://example.test/no",
            )

    def test_raises_when_no_stripe_payment_row(self) -> None:
        from payments.services import StripePaymentService

        donation = DonationFactory(
            payment_method="card",
            payment_status="awaiting_authentication",
        )
        with pytest.raises(ValidationError, match="No StripePayment record"):
            StripePaymentService.send_authentication_link_for_donation(
                donation,
                success_url="https://example.test/ok",
                cancel_url="https://example.test/no",
            )

    @patch("stripe.PaymentIntent.cancel")
    @patch("stripe.checkout.Session.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_creates_checkout_repoints_payment_intent_and_emails_donor(
        self,
        mock_api_key: MagicMock,
        mock_session_create: MagicMock,
        mock_pi_cancel: MagicMock,
    ) -> None:
        from django.core import mail

        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        mock_session = MagicMock()
        mock_session.id = "cs_recovery_001"
        mock_session.payment_intent = "pi_new_intent_001"
        mock_session.url = "https://checkout.stripe.com/c/cs_recovery_001"
        mock_session_create.return_value = mock_session

        donation = DonationFactory(
            payment_method="card",
            payment_status="awaiting_authentication",
        )
        # Donor email comes from the system_donor created by the factory.
        assert donation.system_donor is not None
        donation.system_donor.email = "donor@example.test"
        donation.system_donor.save(update_fields=["email"])

        customer = StripeCustomerFactory(client=donation.campaign.client)
        original_payment = StripePaymentFactory(
            donation=donation,
            stripe_customer=customer,
            stripe_payment_intent_id="pi_original_sca_001",
            status="pending",
            requires_action_url="https://js.stripe.com/v3/3ds/orig",
        )

        result = StripePaymentService.send_authentication_link_for_donation(
            donation,
            success_url="https://example.test/ok",
            cancel_url="https://example.test/no",
        )

        assert result["session_id"] == "cs_recovery_001"
        assert result["checkout_url"] == "https://checkout.stripe.com/c/cs_recovery_001"
        assert result["donor_email"] == "donor@example.test"

        original_payment.refresh_from_db()
        # Checkout-owned PaymentIntent ID is now on the row so the existing
        # ``payment_intent.succeeded`` webhook handler can match it.
        assert original_payment.stripe_payment_intent_id == "pi_new_intent_001"
        assert original_payment.stripe_checkout_session_id == "cs_recovery_001"
        assert original_payment.authentication_link_sent_at is not None

        # Original SCA-stuck intent is cancelled to release any auth hold.
        mock_pi_cancel.assert_called_once_with(
            "pi_original_sca_001", api_key="sk_test_key"
        )

        # An email landed in the locmem outbox addressed to the donor.
        assert len(mail.outbox) == 1
        sent = mail.outbox[0]
        assert sent.to == ["donor@example.test"]
        assert "checkout.stripe.com/c/cs_recovery_001" in sent.body

    @patch("stripe.checkout.Session.create")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_raises_payment_error_when_stripe_session_create_fails(
        self,
        mock_api_key: MagicMock,
        mock_session_create: MagicMock,
    ) -> None:
        from payments.services import StripePaymentError, StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        mock_session_create.side_effect = stripe.StripeError("API down")

        donation = DonationFactory(
            payment_method="card",
            payment_status="awaiting_authentication",
        )
        assert donation.system_donor is not None
        donation.system_donor.email = "donor@example.test"
        donation.system_donor.save(update_fields=["email"])

        customer = StripeCustomerFactory(client=donation.campaign.client)
        StripePaymentFactory(
            donation=donation,
            stripe_customer=customer,
            stripe_payment_intent_id="pi_original_sca_002",
            status="pending",
        )

        with pytest.raises(
            StripePaymentError, match="Failed to create authentication link"
        ):
            StripePaymentService.send_authentication_link_for_donation(
                donation,
                success_url="https://example.test/ok",
                cancel_url="https://example.test/no",
            )
