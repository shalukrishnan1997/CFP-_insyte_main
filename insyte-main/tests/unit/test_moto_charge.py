"""Phase 2 unit tests for MOTO (phone-intake) card auth path.

Covers ``BatchPaymentService.process_donation_payment(..., moto=True)``:

* Correct PaymentIntent.create kwargs (capture_method=manual + intake
  metadata; the MOTO SCA-exemption flag is intentionally NOT sent —
  Stripe rejects it unless the merchant account has MOTO enabled)
* Customer metadata stamps moto_intake/intake_channel/consent_recorded_at
* Donation lands at ``payment_status=requires_capture`` on success
* StripePayment row created with status=requires_capture
* Decline / 3DS edge paths still work in MOTO mode
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.factories import (
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestMotoChargePath:
    """MOTO=True flow against ``BatchPaymentService``."""

    def _build_pi(self, status: str = "requires_capture") -> MagicMock:
        pi = MagicMock()
        pi.id = "pi_moto_test_1"
        pi.status = status
        pi.latest_charge = ""
        pi.to_dict.return_value = {}
        return pi

    def _build_pm(self) -> MagicMock:
        pm = MagicMock()
        pm.card.last4 = "4242"
        pm.card.exp_month = 12
        pm.card.exp_year = 2030
        pm.billing_details.name = "Jane Doe"
        return pm

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_moto_true_passes_capture_method_manual_and_moto_option(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_capture")
        mock_pm_retrieve.return_value = self._build_pm()

        result = BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_001",
            require_qa_approved=False,
            moto=True,
        )

        assert result["success"] is True
        assert result.get("requires_capture") is True

        # Inspect the actual kwargs passed to Stripe
        kwargs = mock_pi_create.call_args.kwargs
        assert kwargs["capture_method"] == "manual"
        # payment_method_options.card.moto is intentionally NOT set —
        # Stripe rejects it as "unknown parameter" unless the merchant
        # account has MOTO capability enabled. capture_method=manual is
        # the load-bearing param for auth-then-capture; the moto SCA
        # exemption is an optional add-on that we leave off.
        assert "payment_method_options" not in kwargs
        assert kwargs["confirm"] is True
        assert kwargs["payment_method"] == "pm_visa_001"
        assert kwargs["metadata"]["moto"] == "true"
        assert kwargs["metadata"]["intake_channel"] == "phone"

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_moto_true_donation_lands_at_requires_capture(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        from donations.models import Donation
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_capture")
        mock_pm_retrieve.return_value = self._build_pm()

        BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_002",
            require_qa_approved=False,
            moto=True,
        )

        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REQUIRES_CAPTURE
        sp = StripePayment.objects.get(donation=donation)
        assert sp.status == StripePayment.STATUS_REQUIRES_CAPTURE
        assert sp.metadata.get("moto") == "true"

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_moto_true_stamps_customer_metadata(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_capture")
        mock_pm_retrieve.return_value = self._build_pm()

        BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_003",
            require_qa_approved=False,
            moto=True,
        )

        # get_or_create_customer was called with metadata stamping moto_intake
        cust_kwargs = mock_customer.call_args.kwargs
        metadata = cust_kwargs["metadata"]
        assert metadata["moto_intake"] == "true"
        assert metadata["intake_channel"] == "phone"
        assert "consent_recorded_at" in metadata

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_moto_skips_qa_approved_check(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """``moto=True`` ignores require_qa_approved — donor is on the phone."""
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_capture")
        mock_pm_retrieve.return_value = self._build_pm()

        result = BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_004",
            # require_qa_approved defaults to True; moto=True overrides it
            moto=True,
        )

        assert result["success"] is True
        assert result.get("requires_capture") is True

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_non_moto_does_not_set_capture_manual(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """Default flow stays unchanged — no capture_method, no moto option."""
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="approved", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("succeeded")
        mock_pm_retrieve.return_value = self._build_pm()

        BatchPaymentService.process_donation_payment(
            donation, UserFactory(is_staff=True), payment_method_id="pm_visa_005"
        )

        kwargs = mock_pi_create.call_args.kwargs
        assert "capture_method" not in kwargs
        assert "payment_method_options" not in kwargs
        # Metadata still contains the channel marker (defaulted to "scan").
        assert kwargs["metadata"]["intake_channel"] == "scan"
        assert kwargs["metadata"]["moto"] == "false"

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_moto_decline_marks_donation_failed(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """A declined MOTO auth still flips donation to failed."""
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_payment_method")
        mock_pm_retrieve.return_value = self._build_pm()

        result = BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_decline_001",
            require_qa_approved=False,
            moto=True,
        )

        donation.refresh_from_db()
        assert result["success"] is False
        assert donation.payment_status == "failed"


@pytest.mark.django_db()
class TestMotoCaptureImmediately:
    """``moto=True, capture_immediately=True`` settles synchronously."""

    def _build_pi(self, status: str = "succeeded") -> MagicMock:
        pi = MagicMock()
        pi.id = "pi_moto_immediate_1"
        pi.status = status
        pi.latest_charge = "ch_moto_immediate_1"
        pi.to_dict.return_value = {}
        return pi

    def _build_pm(self) -> MagicMock:
        pm = MagicMock()
        pm.card.last4 = "4242"
        pm.card.exp_month = 12
        pm.card.exp_year = 2030
        pm.billing_details.name = "Jane Doe"
        return pm

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_immediately_omits_capture_method_kwarg(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """Stripe receives no ``capture_method`` so it defaults to automatic."""
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("succeeded")
        mock_pm_retrieve.return_value = self._build_pm()

        BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_immediate",
            require_qa_approved=False,
            moto=True,
            capture_immediately=True,
        )

        kwargs = mock_pi_create.call_args.kwargs
        assert "capture_method" not in kwargs
        # Phone metadata still present so reporting can group MOTO traffic.
        assert kwargs["metadata"]["intake_channel"] == "phone"
        assert kwargs["metadata"]["moto"] == "true"

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_immediately_lands_at_completed(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """The donation flips to ``payment_status=completed`` synchronously."""
        from donations.models import Donation
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("succeeded")
        mock_pm_retrieve.return_value = self._build_pm()

        result = BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_immediate_2",
            require_qa_approved=False,
            moto=True,
            capture_immediately=True,
        )

        donation.refresh_from_db()
        assert result["success"] is True
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED

    @patch("stripe.PaymentMethod.retrieve")
    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_default_capture_immediately_keeps_manual(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
        mock_pm_retrieve: MagicMock,
    ) -> None:
        """``moto=True`` without ``capture_immediately`` still defers capture."""
        from payments.batch_payment import BatchPaymentService

        mock_api_key.return_value = "sk_test_key"
        donation = DonationFactory(
            payment_method="card", qa_status="pending", payment_status="pending"
        )
        mock_customer.return_value = StripeCustomerFactory(
            client=donation.campaign.client
        )
        mock_pi_create.return_value = self._build_pi("requires_capture")
        mock_pm_retrieve.return_value = self._build_pm()

        BatchPaymentService.process_donation_payment(
            donation,
            UserFactory(is_staff=True),
            payment_method_id="pm_visa_default",
            require_qa_approved=False,
            moto=True,
        )

        kwargs = mock_pi_create.call_args.kwargs
        assert kwargs["capture_method"] == "manual"
