"""Auto-approval at phone intake: eligibility helpers + view promotion.

These tests cover the new "capture at intake + skip QA for happy path"
flow. The full Stripe charge is mocked at the
``BatchPaymentService.process_donation_payment`` boundary so we focus
on the post-charge ``qa_status`` promotion logic rather than re-testing
Stripe call mechanics (those live in ``test_moto_charge.py``).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from donations.intake import (
    PhoneDonationPayload,
    apply_phone_intake_auto_approval,
    is_donation_auto_approve_eligible,
    is_phone_intake_auto_approve_eligible,
)
from donations.models import Donation, DonationBatch
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    SystemDonorFactory,
    UserFactory,
)


def _payload(**overrides: object) -> PhoneDonationPayload:
    base: dict[str, object] = {
        "amount": Decimal("25.00"),
        "currency": "GBP",
        "payment_method": Donation.PAYMENT_METHOD_CARD,
        "donation_date": date.today(),
        "gift_aid": False,
        "donation_frequency": "",
        "donor_source": "house_file",
        "donor_match_status": "exact",
        "card_holder_name": "Jane Doe",
        "card_last_four": "4242",
        "card_expiry_date": "12/30",
        "cheque_number": "",
        "cheque_date": None,
        "caf_voucher_number": "",
        "caf_amount": Decimal("0.00"),
        "postal_order_number": "",
        "postal_order_date": None,
        "sort_code": "",
        "account_number": "",
        "direct_debit_start_date": None,
        "dd_mandate_consent": {},
        "bacs_validation_overridden": False,
    }
    base.update(overrides)
    return cast(PhoneDonationPayload, base)


@pytest.mark.django_db()
class TestIsPhoneIntakeAutoApproveEligible:
    """Truth table for the pre-charge eligibility check."""

    def test_card_with_clean_donor_is_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(payload=_payload(), donor=donor)
            is True
        )

    def test_pending_review_donor_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=True)
        assert (
            is_phone_intake_auto_approve_eligible(payload=_payload(), donor=donor)
            is False
        )

    def test_zero_amount_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(
                payload=_payload(amount=Decimal("0.00")), donor=donor
            )
            is False
        )

    def test_negative_amount_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(
                payload=_payload(amount=Decimal("-5.00")), donor=donor
            )
            is False
        )

    def test_direct_debit_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(
                payload=_payload(payment_method=Donation.PAYMENT_METHOD_DIRECT_DEBIT),
                donor=donor,
            )
            is False
        )

    def test_cheque_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(
                payload=_payload(payment_method=Donation.PAYMENT_METHOD_CHEQUE),
                donor=donor,
            )
            is False
        )

    def test_cash_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        assert (
            is_phone_intake_auto_approve_eligible(
                payload=_payload(payment_method="cash"),
                donor=donor,
            )
            is False
        )


@pytest.mark.django_db()
class TestIsDonationAutoApproveEligible:
    """Same rules but applied to a saved Donation row (webhook recovery path)."""

    def _phone_card_donation(self, **kwargs: Any) -> Donation:
        donor = SystemDonorFactory(pending_review=False)
        defaults: dict = {
            "payment_method": Donation.PAYMENT_METHOD_CARD,
            "amount": Decimal("25.00"),
            "qa_status": Donation.QA_STATUS_PENDING,
            "donor": None,
            "data_file_donor": None,
            "system_donor": donor,
            "field_data": {"intake_method": "phone"},
        }
        defaults.update(kwargs)
        return DonationFactory(**defaults)

    def test_clean_phone_card_donation_is_eligible(self) -> None:
        donation = self._phone_card_donation()
        assert is_donation_auto_approve_eligible(donation) is True

    def test_non_phone_donation_is_not_eligible(self) -> None:
        donation = self._phone_card_donation(field_data={"intake_method": "scan"})
        assert is_donation_auto_approve_eligible(donation) is False

    def test_already_approved_is_not_eligible(self) -> None:
        donation = self._phone_card_donation(qa_status=Donation.QA_STATUS_APPROVED)
        assert is_donation_auto_approve_eligible(donation) is False

    def test_flagged_is_not_eligible(self) -> None:
        donation = self._phone_card_donation(qa_status=Donation.QA_STATUS_FLAGGED)
        assert is_donation_auto_approve_eligible(donation) is False

    def test_zero_amount_is_not_eligible(self) -> None:
        donation = self._phone_card_donation(amount=Decimal("0.00"))
        assert is_donation_auto_approve_eligible(donation) is False

    def test_pending_review_system_donor_is_not_eligible(self) -> None:
        from donors.models import SystemDonor

        donation = self._phone_card_donation()
        SystemDonor.objects.filter(pk=donation.system_donor_id).update(
            pending_review=True
        )
        donation.refresh_from_db()
        assert is_donation_auto_approve_eligible(donation) is False

    def test_non_card_phone_donation_is_not_eligible(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CHEQUE,
            amount=Decimal("25.00"),
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )
        assert is_donation_auto_approve_eligible(donation) is False


@pytest.mark.django_db()
class TestApplyAutoApproval:
    """Saving with update_fields keeps the audit signal diff clean."""

    def test_promotes_qa_status_and_records_note(self) -> None:
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )

        apply_phone_intake_auto_approval(donation, note="Auto-approved at phone intake")

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.qa_notes == "Auto-approved at phone intake"
        assert "auto_approved_at" in donation.field_data
        # intake_method preserved alongside the new key.
        assert donation.field_data.get("intake_method") == "phone"

    def test_also_approves_parent_batch(self) -> None:
        """One-call-one-batch contract: the donation flip closes its batch."""
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )
        batch = donation.batch
        assert batch.status == DonationBatch.STATUS_PENDING_QA

        apply_phone_intake_auto_approval(donation, note="Auto-approved")

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert batch.reviewed_at is not None
        assert "Auto-approved at phone intake" in (batch.review_notes or "")

    def test_already_approved_batch_is_left_alone(self) -> None:
        """Idempotency: re-running on an already-approved batch is a no-op."""
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )
        DonationBatch.objects.filter(pk=donation.batch_id).update(
            status=DonationBatch.STATUS_APPROVED
        )

        apply_phone_intake_auto_approval(donation, note="Auto-approved")

        batch = DonationBatch.objects.get(pk=donation.batch_id)
        assert batch.status == DonationBatch.STATUS_APPROVED


@pytest.mark.django_db(transaction=True)
class TestAutoApprovalQueuesGiftAidTask:
    """The batch flip must queue ``on_batch_approved_task`` on commit.

    Pins the contract that ``apply_phone_intake_auto_approval`` rides
    the existing ``status`` ``post_save`` signal at ``core/signals.py:228``
    — if a future change strips ``"status"`` from ``update_fields``, the
    HMRC Gift Aid CSV silently stops generating. ``transaction=True`` lets
    the ``transaction.on_commit`` hook actually fire.
    """

    @patch("core.tasks.on_batch_approved_task.delay")
    def test_apply_auto_approval_queues_on_batch_approved_task(
        self, mock_on_approved: MagicMock
    ) -> None:
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )

        apply_phone_intake_auto_approval(donation, note="Auto-approved")

        mock_on_approved.assert_called_once()
        called_pk = mock_on_approved.call_args.args[0]
        assert called_pk == donation.batch_id

    @patch("core.tasks.on_batch_approved_task.delay")
    def test_already_approved_batch_does_not_re_queue_task(
        self, mock_on_approved: MagicMock
    ) -> None:
        """Idempotency on the signal level: no duplicate task queued."""
        donor = SystemDonorFactory(pending_review=False)
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_PENDING,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )
        DonationBatch.objects.filter(pk=donation.batch_id).update(
            status=DonationBatch.STATUS_APPROVED
        )

        apply_phone_intake_auto_approval(donation, note="Auto-approved")

        mock_on_approved.assert_not_called()


def _login() -> tuple[Client, object]:
    user = UserFactory(is_staff=True, is_superuser=True)
    http = Client()
    http.force_login(user)
    return http, user


@pytest.mark.django_db()
class TestPhoneIntakeChargeAutoApproval:
    """End-to-end view flow with mocked Stripe — focus on qa_status promotion."""

    def _create_card_donation(
        self, http: Client, *, donor_kwargs: dict | None = None
    ) -> tuple[str, Donation]:
        campaign = CampaignFactory()
        donor = SystemDonorFactory(
            client=campaign.client, **(donor_kwargs or {"pending_review": False})
        )
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "50.00",
                    "payment_method": "card",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "card_holder_name": "Jane Doe",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        donation_id = resp.json()["donation_id"]
        return donation_id, Donation.objects.get(pk=donation_id)

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_clean_card_capture_auto_approves_donation(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id, donation = self._create_card_donation(http)

        # Simulate a synchronous successful capture: the service flips
        # payment_status to "completed" and returns success.
        def fake_charge(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            donation.refresh_from_db()
            donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
            donation.save(update_fields=["payment_status"])
            return {"success": True, "payment_id": "pmt_x"}

        mock_process.side_effect = fake_charge

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {"donation_id": donation_id, "stripe_payment_method_id": "pm_visa"}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["success"] is True
        assert body["auto_approved"] is True
        assert body["qa_status"] == Donation.QA_STATUS_APPROVED
        assert body["payment_status"] == Donation.PAYMENT_STATUS_COMPLETED

        # Verify capture_immediately=True was forwarded.
        kwargs = mock_process.call_args.kwargs
        assert kwargs["capture_immediately"] is True
        assert kwargs["moto"] is True

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert "Auto-approved at phone intake" in donation.qa_notes
        assert "auto_approved_at" in donation.field_data
        # The parent batch is also auto-approved so it skips the QA queue.
        assert donation.batch.status == DonationBatch.STATUS_APPROVED

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_pending_review_donor_does_not_auto_approve(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id, donation = self._create_card_donation(
            http, donor_kwargs={"pending_review": True}
        )

        # Even if Stripe somehow returned completed, eligibility=False
        # means the view does not promote.
        def fake_charge(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            donation.refresh_from_db()
            donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
            donation.save(update_fields=["payment_status"])
            return {"success": True, "payment_id": "pmt_x"}

        mock_process.side_effect = fake_charge

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {"donation_id": donation_id, "stripe_payment_method_id": "pm_visa"}
            ),
            content_type="application/json",
        )
        body = resp.json()
        assert body["success"] is True
        assert body["auto_approved"] is False
        # capture_immediately must be False for pending-review donors
        assert mock_process.call_args.kwargs["capture_immediately"] is False

        donation.refresh_from_db()
        # System-donor flagging makes _resolve_qa_status return FLAGGED.
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_3ds_response_leaves_qa_status_pending(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id, donation = self._create_card_donation(http)

        def fake_charge(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            donation.refresh_from_db()
            donation.payment_status = Donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION
            donation.save(update_fields=["payment_status"])
            return {
                "success": True,
                "awaiting_authentication": True,
                "payment_id": "pmt_x",
            }

        mock_process.side_effect = fake_charge

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {"donation_id": donation_id, "stripe_payment_method_id": "pm_visa"}
            ),
            content_type="application/json",
        )
        body = resp.json()
        assert body["success"] is True
        assert body["auto_approved"] is False
        assert body.get("awaiting_authentication") is True

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_PENDING

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_webhook_beats_view_no_double_write_to_qa_notes(
        self, mock_process: MagicMock
    ) -> None:
        """Race regression: webhook auto-approves between charge and refresh.

        The view's promotion branch is gated on ``qa_status=pending`` so
        the existing ``qa_notes`` ("Auto-approved post-3DS authentication")
        is preserved. The response still reports ``auto_approved=true``
        so the operator UI renders the same success card.
        """
        http, _ = _login()
        donation_id, donation = self._create_card_donation(http)

        webhook_note = "Auto-approved post-3DS authentication"

        def fake_charge_and_webhook(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            # Simulate: charge succeeds AND the webhook auto-approves
            # the donation before the view's refresh_from_db runs.
            donation.refresh_from_db()
            donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
            donation.qa_status = Donation.QA_STATUS_APPROVED
            donation.qa_notes = webhook_note
            donation.save(update_fields=["payment_status", "qa_status", "qa_notes"])
            return {"success": True, "payment_id": "pmt_x"}

        mock_process.side_effect = fake_charge_and_webhook

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {"donation_id": donation_id, "stripe_payment_method_id": "pm_visa"}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["success"] is True
        assert body["auto_approved"] is True
        assert body["qa_status"] == Donation.QA_STATUS_APPROVED

        donation.refresh_from_db()
        # Critical: the view did NOT overwrite the webhook's notes.
        assert donation.qa_notes == webhook_note

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_declined_card_leaves_qa_status_pending(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id, donation = self._create_card_donation(http)

        mock_process.return_value = {"success": False, "error": "Card declined"}

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {"donation_id": donation_id, "stripe_payment_method_id": "pm_visa"}
            ),
            content_type="application/json",
        )
        # Decline returns 200 with success=False (per existing API contract).
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["auto_approved"] is False

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_PENDING


@pytest.mark.django_db()
class TestPhoneIntakeRefundView:
    """Same-session refund button restricted to recent self-owned donations."""

    def _make_charged_phone_donation(
        self, http: Client, operator_user: Any, *, hours_ago: int = 0
    ) -> Donation:
        from datetime import timedelta

        from django.utils import timezone

        from payments.models import StripeCustomer, StripePayment

        donor = SystemDonorFactory(pending_review=False)
        batch = DonationBatchFactory()
        donation = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            filled_by=operator_user,
            payment_method=Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_APPROVED,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            donor=None,
            data_file_donor=None,
            system_donor=donor,
            field_data={"intake_method": "phone"},
        )
        if hours_ago:
            new_created = timezone.now() - timedelta(hours=hours_ago)
            Donation.objects.filter(pk=donation.pk).update(created_at=new_created)
            donation.refresh_from_db()

        stripe_customer = StripeCustomer.objects.create(
            client=batch.campaign.client,
            stripe_customer_id="cus_test",
        )
        StripePayment.objects.create(
            stripe_payment_intent_id="pi_test",
            stripe_customer=stripe_customer,
            donation=donation,
            amount=donation.amount,
            currency=donation.currency,
            status=StripePayment.STATUS_SUCCEEDED,
            description="Test",
            processed_by=operator_user,
        )
        return donation

    @patch("payments.services.StripePaymentService.refund_payment")
    def test_operator_can_refund_own_recent_donation(
        self, mock_refund: MagicMock
    ) -> None:
        http, operator = _login()
        donation = self._make_charged_phone_donation(http, operator)

        # Mimic the production refund_payment side-effect: it flips
        # donation.payment_status to refunded inside the same transaction
        # (payments/services.py:941-943) BEFORE the view refreshes. Pin
        # that ordering — without it, the view's qa_status=pending save
        # would briefly co-exist with payment_status=completed and risk
        # a thank-you letter for refunded money.
        def fake_refund(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            donation.payment_status = Donation.PAYMENT_STATUS_REFUNDED
            donation.save(update_fields=["payment_status"])
            return {"success": True, "refund_id": "re_x"}

        mock_refund.side_effect = fake_refund

        resp = http.post(
            reverse("custom_admin:phone_intake_refund"),
            data=json.dumps(
                {"donation_id": str(donation.id), "reason": "fat-fingered amount"}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["success"] is True
        assert body["qa_status"] == Donation.QA_STATUS_PENDING

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.payment_status == Donation.PAYMENT_STATUS_REFUNDED
        assert "Refunded at intake" in donation.qa_notes

    @patch("payments.services.StripePaymentService.refund_payment")
    def test_other_operator_cannot_refund(self, mock_refund: MagicMock) -> None:
        http, _operator = _login()
        # Donation owned by a *different* operator
        other = UserFactory(is_staff=True)
        donation = self._make_charged_phone_donation(http, other)

        resp = http.post(
            reverse("custom_admin:phone_intake_refund"),
            data=json.dumps({"donation_id": str(donation.id)}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        mock_refund.assert_not_called()

    @patch("payments.services.StripePaymentService.refund_payment")
    def test_donation_older_than_24h_rejected(self, mock_refund: MagicMock) -> None:
        http, operator = _login()
        donation = self._make_charged_phone_donation(http, operator, hours_ago=30)

        resp = http.post(
            reverse("custom_admin:phone_intake_refund"),
            data=json.dumps({"donation_id": str(donation.id)}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "24h" in resp.json()["error"]
        mock_refund.assert_not_called()

    @patch("payments.services.StripePaymentService.refund_payment")
    def test_non_phone_donation_rejected(self, mock_refund: MagicMock) -> None:
        http, operator = _login()
        donation = self._make_charged_phone_donation(http, operator)
        donation.field_data = {"intake_method": "scan"}
        donation.save(update_fields=["field_data"])

        resp = http.post(
            reverse("custom_admin:phone_intake_refund"),
            data=json.dumps({"donation_id": str(donation.id)}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        mock_refund.assert_not_called()
