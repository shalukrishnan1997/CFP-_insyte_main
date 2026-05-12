"""Phase 2 unit tests for MOTO capture/cancel at QA approval/rejection."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests.factories import (
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)


def _make_intent(status: str = "succeeded", latest_charge: str = "ch_x") -> MagicMock:
    pi = MagicMock()
    pi.id = "pi_capture_test"
    pi.status = status
    pi.latest_charge = latest_charge
    pi.to_dict.return_value = {}
    return pi


@pytest.mark.django_db()
class TestCapturePaymentIntent:
    """``StripePaymentService.capture_payment_intent``."""

    def _seed_requires_capture(
        self, *, donation_kwargs=None, payment_intent_id="pi_cap_001"
    ):
        from payments.models import StripePayment

        donation_kwargs = donation_kwargs or {}
        donation = DonationFactory(
            payment_method="card",
            qa_status="pending",
            payment_status="requires_capture",
            **donation_kwargs,
        )
        customer = StripeCustomerFactory(client=donation.campaign.client)
        sp = StripePayment.objects.create(
            stripe_payment_intent_id=payment_intent_id,
            stripe_customer=customer,
            donation=donation,
            amount=donation.amount,
            currency=donation.currency,
            status=StripePayment.STATUS_REQUIRES_CAPTURE,
            description="MOTO test",
            metadata={"moto": "true"},
            processed_by=UserFactory(is_staff=True),
        )
        return donation, sp

    @patch("stripe.PaymentIntent.capture")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_flips_donation_to_completed(
        self,
        mock_api_key: MagicMock,
        mock_capture: MagicMock,
    ) -> None:
        from donations.models import Donation
        from payments.models import StripePayment
        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        donation, sp = self._seed_requires_capture()
        mock_capture.return_value = _make_intent(
            status="succeeded", latest_charge="ch_capture_x"
        )

        result = StripePaymentService.capture_payment_intent(str(sp.id))

        assert result["success"] is True
        donation.refresh_from_db()
        sp.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED
        assert sp.status == StripePayment.STATUS_SUCCEEDED
        assert sp.stripe_charge_id == "ch_capture_x"

        # Idempotency key derived from the payment id
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["idempotency_key"] == f"capture_{sp.id}"

    @patch("stripe.PaymentIntent.capture")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_already_succeeded_is_idempotent_skip(
        self,
        mock_api_key: MagicMock,
        mock_capture: MagicMock,
    ) -> None:
        from payments.models import StripePayment
        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        _donation, sp = self._seed_requires_capture()
        sp.status = StripePayment.STATUS_SUCCEEDED
        sp.save(update_fields=["status"])

        result = StripePaymentService.capture_payment_intent(str(sp.id))
        assert result["success"] is True
        assert result.get("skipped") is True
        # No Stripe call when already captured
        mock_capture.assert_not_called()

    @patch("stripe.PaymentIntent.capture")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_stripe_error_marks_failed(
        self,
        mock_api_key: MagicMock,
        mock_capture: MagicMock,
    ) -> None:
        import stripe

        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        donation, sp = self._seed_requires_capture()
        mock_capture.side_effect = stripe.StripeError("auth expired")

        result = StripePaymentService.capture_payment_intent(str(sp.id))

        assert result["success"] is False
        donation.refresh_from_db()
        sp.refresh_from_db()
        assert donation.payment_status == "failed"
        assert sp.status == "failed"

    @patch("payments.services.StripePaymentService._get_api_key")
    def test_capture_wrong_payment_status_returns_error(
        self,
        mock_api_key: MagicMock,
    ) -> None:
        from payments.models import StripePayment
        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        _donation, sp = self._seed_requires_capture()
        sp.status = StripePayment.STATUS_FAILED
        sp.save(update_fields=["status"])

        result = StripePaymentService.capture_payment_intent(str(sp.id))
        assert result["success"] is False
        assert "cannot be captured" in result["error"]


@pytest.mark.django_db()
class TestCancelPaymentIntent:
    """``StripePaymentService.cancel_payment_intent``."""

    def _seed_requires_capture(self):
        from payments.models import StripePayment

        donation = DonationFactory(
            payment_method="card",
            qa_status="pending",
            payment_status="requires_capture",
        )
        customer = StripeCustomerFactory(client=donation.campaign.client)
        sp = StripePayment.objects.create(
            stripe_payment_intent_id="pi_cancel_001",
            stripe_customer=customer,
            donation=donation,
            amount=donation.amount,
            currency=donation.currency,
            status=StripePayment.STATUS_REQUIRES_CAPTURE,
            description="MOTO test",
            metadata={"moto": "true"},
            processed_by=UserFactory(is_staff=True),
        )
        return donation, sp

    @patch("stripe.PaymentIntent.cancel")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_cancel_flips_donation_to_failed(
        self,
        mock_api_key: MagicMock,
        mock_cancel: MagicMock,
    ) -> None:
        from payments.models import StripePayment
        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        donation, sp = self._seed_requires_capture()

        result = StripePaymentService.cancel_payment_intent(
            str(sp.id), reason="qa_rejected"
        )

        assert result["success"] is True
        donation.refresh_from_db()
        sp.refresh_from_db()
        assert donation.payment_status == "failed"
        assert sp.status == StripePayment.STATUS_CANCELLED

        kwargs = mock_cancel.call_args.kwargs
        assert kwargs["idempotency_key"] == f"cancel_{sp.id}"

    @patch("stripe.PaymentIntent.cancel")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_cancel_already_cancelled_is_idempotent_skip(
        self,
        mock_api_key: MagicMock,
        mock_cancel: MagicMock,
    ) -> None:
        from payments.models import StripePayment
        from payments.services import StripePaymentService

        mock_api_key.return_value = "sk_test_key"
        _, sp = self._seed_requires_capture()
        sp.status = StripePayment.STATUS_CANCELLED
        sp.save(update_fields=["status"])

        result = StripePaymentService.cancel_payment_intent(str(sp.id))
        assert result["success"] is True
        assert result.get("skipped") is True
        mock_cancel.assert_not_called()


def _required_qa_post_fields() -> dict[str, str]:
    """Match the QA form's per-donation required fields helper."""
    return {
        "donor_title": "Mr",
        "donor_first_name": "John",
        "donor_last_name": "Doe",
        "package_code": "PKG01",
        "donor_address_line1": "10 High Street",
        "donor_postcode": "SW1A 1AA",
    }


@pytest.mark.django_db()
class TestHandleQaActionMotoIntegration:
    """Integration tests: ``_handle_qa_action`` drives capture/cancel."""

    def _seed_batch_with_moto_donation(self):
        from payments.models import StripePayment
        from tests.factories import DonationBatchFactory

        user = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory()
        donation = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            payment_method="card",
            payment_status="requires_capture",
            qa_status="pending",
            amount=Decimal("60.00"),
        )
        customer = StripeCustomerFactory(client=donation.campaign.client)
        sp = StripePayment.objects.create(
            stripe_payment_intent_id=f"pi_h_{donation.id}",
            stripe_customer=customer,
            donation=donation,
            amount=donation.amount,
            currency=donation.currency,
            status=StripePayment.STATUS_REQUIRES_CAPTURE,
            description="moto test",
            metadata={"moto": "true"},
            processed_by=user,
        )
        return batch, donation, sp, user

    @patch("payments.services.StripePaymentService.capture_payment_intent")
    def test_per_donation_approve_captures_moto_auth(
        self, mock_capture: MagicMock
    ) -> None:
        from django.test import RequestFactory

        from custom_admin.views import qa_review

        batch, donation, sp, user = self._seed_batch_with_moto_donation()
        mock_capture.return_value = {"success": True, "payment_id": str(sp.id)}

        rf = RequestFactory()
        request = rf.post(
            "/admin/qa/",
            data={"action": "approve", **_required_qa_post_fields()},
        )
        request.user = user
        from django.contrib.messages.storage.fallback import FallbackStorage

        request.session = {}
        request._messages = FallbackStorage(request)
        qa_review._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]

        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, batch, donation, next_id=None
        )

        mock_capture.assert_called_once_with(str(sp.id))

    @patch("payments.services.StripePaymentService.cancel_payment_intent")
    def test_per_donation_reject_cancels_moto_auth(
        self, mock_cancel: MagicMock
    ) -> None:
        from django.test import RequestFactory

        from custom_admin.views import qa_review

        batch, donation, sp, user = self._seed_batch_with_moto_donation()
        mock_cancel.return_value = {"success": True, "payment_id": str(sp.id)}

        rf = RequestFactory()
        request = rf.post(
            "/admin/qa/",
            data={
                "action": "reject",
                "qa_notes": "donor changed their mind",
                "qa_reject_reason": "other",
                **_required_qa_post_fields(),
            },
        )
        request.user = user
        from django.contrib.messages.storage.fallback import FallbackStorage

        request.session = {}
        request._messages = FallbackStorage(request)
        qa_review._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]

        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, batch, donation, next_id=None
        )

        mock_cancel.assert_called_once()
        args, _kwargs = mock_cancel.call_args
        assert args[0] == str(sp.id)


@pytest.mark.django_db()
class TestQaApprovalCapturesMotoAuth:
    """The QA approval path captures requires_capture donations."""

    @patch("payments.services.StripePaymentService.capture_payment_intent")
    def test_batch_approve_captures_moto_donations_in_batch(
        self,
        mock_capture: MagicMock,
    ) -> None:
        """``_capture_moto_auths_for_batch`` calls capture for each MOTO row."""
        from custom_admin.views.qa_review import _capture_moto_auths_for_batch
        from payments.models import StripePayment
        from tests.factories import DonationBatchFactory

        batch = DonationBatchFactory()
        donations = [
            DonationFactory(
                campaign=batch.campaign,
                batch=batch,
                payment_method="card",
                payment_status="requires_capture",
                qa_status="pending",
                amount=Decimal("10.00"),
            )
            for _ in range(3)
        ]
        for d in donations:
            customer = StripeCustomerFactory(client=d.campaign.client)
            StripePayment.objects.create(
                stripe_payment_intent_id=f"pi_batch_{d.id}",
                stripe_customer=customer,
                donation=d,
                amount=d.amount,
                currency=d.currency,
                status=StripePayment.STATUS_REQUIRES_CAPTURE,
                description="batch test",
                metadata={"moto": "true"},
                processed_by=UserFactory(is_staff=True),
            )
        mock_capture.return_value = {"success": True, "payment_id": "x"}

        _capture_moto_auths_for_batch(batch)

        assert mock_capture.call_count == 3

    @patch("payments.services.StripePaymentService.capture_payment_intent")
    def test_capture_failure_reverts_donation_to_flagged(
        self,
        mock_capture: MagicMock,
    ) -> None:
        """Donations whose capture fails must NOT remain at qa_status=approved.

        Approved + payment_status=failed would slip into the letter
        generator's eligibility queryset and produce a thank-you letter for
        a payment that never settled. The fix reverts those rows to
        ``qa_status=flagged`` so they stay in the QA queue.
        """
        from custom_admin.views.qa_review import _capture_moto_auths_for_batch
        from donations.models import Donation
        from letters.tasks import build_letter_generation_queryset
        from payments.models import StripePayment
        from tests.factories import DonationBatchFactory

        batch = DonationBatchFactory()
        good = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            payment_method="card",
            payment_status="requires_capture",
            qa_status="approved",  # cascade has already run
            amount=Decimal("10.00"),
        )
        bad = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            payment_method="card",
            payment_status="requires_capture",
            qa_status="approved",
            amount=Decimal("20.00"),
        )
        bad_payment_id: str | None = None
        for d in (good, bad):
            customer = StripeCustomerFactory(client=d.campaign.client)
            sp = StripePayment.objects.create(
                stripe_payment_intent_id=f"pi_fail_{d.id}",
                stripe_customer=customer,
                donation=d,
                amount=d.amount,
                currency=d.currency,
                status=StripePayment.STATUS_REQUIRES_CAPTURE,
                description="t",
                metadata={"moto": "true"},
                processed_by=UserFactory(is_staff=True),
            )
            if d is bad:
                bad_payment_id = str(sp.id)

        # Order-agnostic: route by StripePayment.id rather than ordered side_effect.
        def _capture_router(stripe_payment_id: str) -> dict:
            if stripe_payment_id == bad_payment_id:
                return {"success": False, "error": "card_declined_at_capture"}
            return {"success": True, "payment_id": stripe_payment_id}

        mock_capture.side_effect = _capture_router

        failures = _capture_moto_auths_for_batch(batch)

        assert len(failures) == 1
        assert failures[0].id == bad.id

        bad.refresh_from_db()
        assert bad.qa_status == Donation.QA_STATUS_FLAGGED, (
            "Failed-capture donation must be reverted to flagged so it stays "
            "in the QA queue and never reaches the letter generator."
        )

        # Letter eligibility seam: the bad donation must be excluded.
        eligible = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = {str(pk) for pk in eligible.values_list("id", flat=True)}
        assert str(bad.id) not in eligible_ids
        # The successful one stays approved and remains letter-eligible.
        assert str(good.id) in eligible_ids

    @patch("payments.services.StripePaymentService.capture_payment_intent")
    def test_capture_with_missing_stripe_payment_reverts_to_flagged(
        self,
        mock_capture: MagicMock,
    ) -> None:
        """Defence in depth: a requires_capture donation with no StripePayment
        row is a corrupt state that should NOT silently approve through to
        letters.
        """
        from custom_admin.views.qa_review import _capture_moto_auths_for_batch
        from donations.models import Donation
        from tests.factories import DonationBatchFactory

        batch = DonationBatchFactory()
        donation = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            payment_method="card",
            payment_status="requires_capture",
            qa_status="approved",
            amount=Decimal("10.00"),
        )
        # No StripePayment row created — simulating the corrupt state.

        failures = _capture_moto_auths_for_batch(batch)

        assert len(failures) == 1
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        # Stripe was never called because there was no payment row.
        mock_capture.assert_not_called()
