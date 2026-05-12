"""Unit tests for ``BankingService.reverse_donations_for_slip``.

FIN-BANK-REVERSAL-* test cases covering automatic reversal of donation
``payment_status`` when bank processing flags a slip as ``issues`` /
``partial_success`` / ``failed`` (cheque bounce, cash count short).

The pre-existing flow only updated the slip itself — donations stayed
``completed`` and operators had no audit-complete way to roll them back.
``BankingService.reverse_donations_for_slip`` closes that gap and the view
hook in ``slip_record_processing`` triggers it automatically.

FIN-BANK-REVERSAL-CASCADE-* extends those mechanics: when a reversed
donation already had a thank-you letter generated/sent or sat in a
submitted Gift Aid CSV, the cascade flags it (``letter_voided_at``) and
fans out notifications to the people who can act on it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from banking.models import PayingInSlip
from banking.services import BankingService
from donations.models import Donation, DonationBatch
from notifications.models import Notification
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    UserFactory,
)


def _make_completed_donation(
    slip: PayingInSlip, *, amount: Decimal, payment_method: str
) -> Donation:
    """Build a settled donation linked to ``slip``.

    Card payments only count toward the slip when ``payment_status='completed'``;
    other payment methods (cheque, cash, etc.) are settled regardless. This
    helper paints the row as completed in either case so the reversal target
    set is unambiguous.
    """
    campaign = CampaignFactory(client=slip.client, status="active")
    return DonationFactory(
        campaign=campaign,
        batch=DonationBatchFactory(campaign=campaign),
        amount=amount,
        payment_method=payment_method,
        payment_status=Donation.PAYMENT_STATUS_COMPLETED,
        qa_status="approved",
        paying_in_slip=slip,
    )


# ═══════════════════════════════════════════════════════════════
# BankingService.reverse_donations_for_slip — service-level
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestReverseDonationsForSlipService:
    """FIN-BANK-REVERSAL-001 to 004: service method behaviour."""

    def test_cheque_bounce_reverses_all_cheque_donations(self) -> None:
        """FIN-BANK-REVERSAL-001: 5 cheque donations all flip to 'reversed'."""
        slip = PayingInSlipFactory(payment_type="cheque")
        donations = [
            _make_completed_donation(
                slip, amount=Decimal("50.00"), payment_method="cheque"
            )
            for _ in range(5)
        ]

        count = BankingService.reverse_donations_for_slip(
            slip, reason="Cheque bounced — insufficient funds"
        )

        assert count == 5
        for donation in donations:
            donation.refresh_from_db()
            assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
            assert (
                donation.payment_reversal_reason
                == "Cheque bounced — insufficient funds"
            )

    def test_cash_short_reverses_all_completed_donations(self) -> None:
        """FIN-BANK-REVERSAL-002: cash short reverses all settled rows.

        The service walks every settled donation on the slip — banking has
        no way to know which specific cash donations made up the shortfall,
        so the safe default is to reverse all of them and let operators
        re-bank what was actually deposited.
        """
        slip = PayingInSlipFactory(payment_type="cash")
        cash_donations = [
            _make_completed_donation(
                slip, amount=Decimal("10.00"), payment_method="cash"
            )
            for _ in range(3)
        ]
        # Add an unsettled card row to confirm it is left untouched — its
        # ``payment_status`` is ``pending`` so the filter excludes it anyway.
        campaign = CampaignFactory(client=slip.client, status="active")
        unsettled_card = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("999.00"),
            payment_method="card",
            payment_status=Donation.PAYMENT_STATUS_PENDING,
            qa_status="approved",
            paying_in_slip=slip,
        )

        count = BankingService.reverse_donations_for_slip(
            slip, reason="Cash count short by £5"
        )

        assert count == 3
        for donation in cash_donations:
            donation.refresh_from_db()
            assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
            assert donation.payment_reversal_reason == "Cash count short by £5"
        unsettled_card.refresh_from_db()
        assert unsettled_card.payment_status == Donation.PAYMENT_STATUS_PENDING
        assert unsettled_card.payment_reversal_reason == ""

    def test_reversal_is_idempotent(self) -> None:
        """FIN-BANK-REVERSAL-003: rerunning reversal is a no-op.

        ``reverse_donations_for_slip`` only targets rows still at
        ``payment_status='completed'``. Once flipped to ``reversed`` they fall
        out of the filter, so a second call returns 0 and does not overwrite
        the existing reason.
        """
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("75.00"), payment_method="cheque"
        )

        first_count = BankingService.reverse_donations_for_slip(
            slip, reason="First reason"
        )
        second_count = BankingService.reverse_donations_for_slip(
            slip, reason="Different reason on rerun"
        )

        assert first_count == 1
        assert second_count == 0
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
        # The first call's reason wins — second call sees no targets to update.
        assert donation.payment_reversal_reason == "First reason"

    def test_reversal_emits_audit_log_per_donation(self) -> None:
        """FIN-BANK-REVERSAL-004: audit signal fires for each reversed row.

        ``Donation`` is in ``AUDITED_MODELS`` so ``audit_post_save`` records
        the ``payment_status`` field-level diff automatically — the service
        does not need to write audit entries by hand.
        """
        slip = PayingInSlipFactory()
        donations = [
            _make_completed_donation(
                slip, amount=Decimal("20.00"), payment_method="cheque"
            )
            for _ in range(2)
        ]

        BankingService.reverse_donations_for_slip(slip, reason="Cheque dishonoured")

        donation_ids = {str(d.id) for d in donations}
        update_logs = AuditLog.objects.filter(
            model_name="Donation",
            action="UPDATE",
            object_id__in=donation_ids,
        )
        # Each reversed donation should have at least one UPDATE audit row
        # whose ``changes`` mentions ``payment_status``.
        seen_ids = {
            log.object_id
            for log in update_logs
            if "payment_status" in (log.changes or {})
        }
        assert seen_ids == donation_ids

    def test_reversal_skips_non_completed_rows(self) -> None:
        """FIN-BANK-REVERSAL-005: pending/failed/refunded rows are untouched."""
        slip = PayingInSlipFactory()
        campaign = CampaignFactory(client=slip.client, status="active")
        # All linked, but only the completed one is reversed.
        completed = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("40.00"),
            payment_method="cheque",
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status="approved",
            paying_in_slip=slip,
        )
        pending = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("40.00"),
            payment_method="card",
            payment_status=Donation.PAYMENT_STATUS_PENDING,
            qa_status="approved",
            paying_in_slip=slip,
        )
        already_failed = DonationFactory(
            campaign=campaign,
            batch=DonationBatchFactory(campaign=campaign),
            amount=Decimal("40.00"),
            payment_method="card",
            payment_status=Donation.PAYMENT_STATUS_FAILED,
            qa_status="approved",
            paying_in_slip=slip,
        )

        count = BankingService.reverse_donations_for_slip(slip, reason="bounce")

        assert count == 1
        completed.refresh_from_db()
        pending.refresh_from_db()
        already_failed.refresh_from_db()
        assert completed.payment_status == Donation.PAYMENT_STATUS_REVERSED
        assert pending.payment_status == Donation.PAYMENT_STATUS_PENDING
        assert already_failed.payment_status == Donation.PAYMENT_STATUS_FAILED


# ═══════════════════════════════════════════════════════════════
# slip_record_processing view — auto-reversal hook
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestSlipRecordProcessingTriggersReversal:
    """FIN-BANK-REVERSAL-010 to 012: view triggers reversal automatically."""

    @staticmethod
    def _login_staff_client() -> Client:
        user = UserFactory(is_staff=True, is_superuser=True)
        user.set_password("testpass123!")
        user.save(update_fields=["password"])
        client = Client()
        assert client.login(username=user.username, password="testpass123!")
        return client

    def _post_processing(
        self,
        client: Client,
        slip_id: int,
        *,
        completion_status: str,
        processed_amount: str,
        processing_issues: list[str] | None = None,
        custom_issue: str = "",
    ) -> dict[str, object]:
        response = client.post(
            reverse("custom_admin:slip_record_processing", kwargs={"slip_id": slip_id}),
            {
                "processed_amount": processed_amount,
                "completion_status": completion_status,
                "bank_processed_date": "2026-04-27",
                "processing_issues": json.dumps(processing_issues or []),
                "custom_issue": custom_issue,
            },
        )
        assert response.status_code == 200, response.content
        return json.loads(response.content)

    def test_issues_completion_status_reverses_donations(self) -> None:
        """FIN-BANK-REVERSAL-010: completion_status='issues' triggers reversal."""
        client = self._login_staff_client()
        slip = PayingInSlipFactory(status="ready", total_amount=Decimal("150.00"))
        donation = _make_completed_donation(
            slip, amount=Decimal("150.00"), payment_method="cheque"
        )

        payload = self._post_processing(
            client,
            slip.pk,
            completion_status="issues",
            processed_amount="0.00",
            processing_issues=["invalid_cheque"],
            custom_issue="Cheque bounced",
        )

        assert payload["success"] is True
        assert payload["reversed_donation_count"] == 1
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
        assert "invalid_cheque" in donation.payment_reversal_reason
        assert "Cheque bounced" in donation.payment_reversal_reason

    def test_partial_success_completion_status_reverses_donations(self) -> None:
        """FIN-BANK-REVERSAL-011: completion_status='partial_success' reverses.

        With cash discrepancies, banking can't cherry-pick which specific
        donations made up the shortfall — every settled row on the slip
        is reversed and the operator re-banks the verified subset.
        """
        client = self._login_staff_client()
        slip = PayingInSlipFactory(status="ready", total_amount=Decimal("100.00"))
        donations = [
            _make_completed_donation(
                slip, amount=Decimal("25.00"), payment_method="cash"
            )
            for _ in range(4)
        ]

        payload = self._post_processing(
            client,
            slip.pk,
            completion_status="partial_success",
            processed_amount="75.00",
            processing_issues=["incorrect_amount"],
            custom_issue="Cash short by £25",
        )

        assert payload["success"] is True
        assert payload["reversed_donation_count"] == 4
        for donation in donations:
            donation.refresh_from_db()
            assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED

    def test_full_success_completion_status_does_not_reverse(self) -> None:
        """FIN-BANK-REVERSAL-012: full_success leaves donations completed."""
        client = self._login_staff_client()
        slip = PayingInSlipFactory(status="ready", total_amount=Decimal("100.00"))
        donation = _make_completed_donation(
            slip, amount=Decimal("100.00"), payment_method="cheque"
        )

        payload = self._post_processing(
            client,
            slip.pk,
            completion_status="full_success",
            processed_amount="100.00",
        )

        assert payload["success"] is True
        assert payload["reversed_donation_count"] == 0
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED


@pytest.mark.django_db(transaction=True)
class TestSlipRecordProcessingNotifiesProcessor:
    """FIN-BANK-REVERSAL-CASCADE-V01: end-to-end cascade-fanout via the view.

    Regression guard for an ordering bug: the view used to call
    ``reverse_donations_for_slip`` *before* ``mark_slip_as_processed``,
    so the cascade's ``transaction.on_commit`` callback fired before
    ``slip.processed_by`` was populated and the slip processor never
    received a notification. The view now wraps both calls in a single
    ``transaction.atomic`` so the cascade defers until processed_by is
    set; this test would fail (zero notifications for the staff user)
    if the ordering or atomic wrapping regresses.
    """

    @patch("core.tasks.send_batch_status_email")
    def test_processor_is_notified_via_view_path(self, _mock_email: Any) -> None:
        staff_user = UserFactory(
            is_staff=True, is_superuser=True, email="staff@example.com"
        )
        staff_user.set_password("testpass123!")
        staff_user.save(update_fields=["password"])
        client = Client()
        assert client.login(username=staff_user.username, password="testpass123!")

        slip = PayingInSlipFactory(
            status="ready", total_amount=Decimal("50.00"), processed_by=None
        )
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="cheque")

        response = client.post(
            reverse(
                "custom_admin:slip_record_processing",
                kwargs={"slip_id": slip.pk},
            ),
            {
                "processed_amount": "0.00",
                "completion_status": "issues",
                "bank_processed_date": "2026-04-27",
                "processing_issues": json.dumps(["invalid_cheque"]),
                "custom_issue": "Cheque bounced",
            },
        )
        assert response.status_code == 200, response.content

        slip.refresh_from_db()
        assert slip.processed_by_id == staff_user.id
        # Cascade must notify the processor — without the atomic wrap, this
        # count would be 0 because the on_commit lambda fired before
        # mark_slip_as_processed set processed_by.
        notifications = Notification.objects.filter(user=staff_user)
        assert notifications.count() >= 1
        notif = notifications.first()
        assert notif is not None
        assert "Banking reversal" in notif.title


# ═══════════════════════════════════════════════════════════════
# BankingService.reverse_donations_for_slip — empty / edge cases
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestReverseDonationsForSlipEdgeCases:
    """FIN-BANK-REVERSAL-020: empty slip and no-op behaviour."""

    def test_reversal_on_empty_slip_returns_zero(self) -> None:
        """FIN-BANK-REVERSAL-020: empty slip is a no-op, returns 0."""
        slip = PayingInSlipFactory()

        count = BankingService.reverse_donations_for_slip(
            slip, reason="nothing to reverse"
        )

        assert count == 0

    def test_reversal_reason_stored_verbatim(self) -> None:
        """FIN-BANK-REVERSAL-021: reason text preserved exactly."""
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("12.34"), payment_method="cheque"
        )
        verbose_reason = (
            "Cheque #001234 returned by Lloyds 2026-04-27: "
            "stop-payment instruction received from drawer."
        )

        BankingService.reverse_donations_for_slip(slip, reason=verbose_reason)

        donation.refresh_from_db()
        assert donation.payment_reversal_reason == verbose_reason


# ═══════════════════════════════════════════════════════════════
# FIN-BANK-REVERSAL-CASCADE — letter void + Gift Aid awareness
# ═══════════════════════════════════════════════════════════════


def _attach_letter_batch(donation: Donation, *, status: str, printer: Any) -> None:
    """Attach a generated/sent LetterBatch to a donation.

    Sets ``letter_status``, ``letter_generated_at`` and ``letter_batch`` on
    the donation. ``status`` should be ``"generated"`` or ``"sent"`` to
    represent a letter the cascade should void.
    """
    from letters.models import LetterBatch, LetterTemplate

    template = LetterTemplate.objects.create(
        name=f"Thanks template for {donation.campaign.name}",
        template_type=LetterTemplate.TEMPLATE_TYPE_THANK_YOU,
        campaign=donation.campaign,
        file=SimpleUploadedFile("template.docx", b"docx-bytes"),
        created_by=printer,
    )
    letter_batch = LetterBatch.objects.create(
        campaign=donation.campaign,
        template=template,
        batch_number=1,
        status=LetterBatch.STATUS_COMPLETED,
        created_by=printer,
    )
    donation.letter_batch = letter_batch
    donation.letter_status = status
    donation.letter_generated_at = timezone.now()
    donation.save(
        update_fields=["letter_batch", "letter_status", "letter_generated_at"]
    )


@pytest.mark.django_db()
class TestReverseDonationsCascadeFieldUpdates:
    """FIN-BANK-REVERSAL-CASCADE-001 to 004: letter-void field cascade.

    These tests verify the synchronous part of the cascade — what
    ``reverse_donations_for_slip`` writes to ``Donation`` rows. The async
    notification fan-out runs via ``transaction.on_commit`` and is covered
    in ``TestReverseDonationsCascadeNotifications`` below with
    ``transaction=True`` so on-commit hooks actually fire.
    """

    def test_reversal_voids_generated_letter(self) -> None:
        """FIN-BANK-REVERSAL-CASCADE-001: letter_status='generated' → voided.

        ``letter_status`` itself is preserved — the cascade adds
        ``letter_voided_at`` so reports can distinguish "letter went out and
        is now invalid" from "no letter ever produced".
        """
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("50.00"), payment_method="cheque"
        )
        printer = UserFactory()
        _attach_letter_batch(donation, status="generated", printer=printer)

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        donation.refresh_from_db()
        assert donation.letter_voided_at is not None
        assert donation.letter_void_reason == "Cheque bounce"
        # letter_status preserved — physical letter was issued, that fact stays.
        assert donation.letter_status == "generated"

    def test_reversal_voids_sent_letter(self) -> None:
        """FIN-BANK-REVERSAL-CASCADE-002: letter_status='sent' → voided too."""
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("50.00"), payment_method="cheque"
        )
        printer = UserFactory()
        _attach_letter_batch(donation, status="sent", printer=printer)

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        donation.refresh_from_db()
        assert donation.letter_voided_at is not None
        assert donation.letter_status == "sent"

    def test_reversal_does_not_void_pending_letter(self) -> None:
        """FIN-BANK-REVERSAL-CASCADE-003: pending letters are not flagged.

        Donations without a generated letter have nothing to void —
        ``letter_voided_at`` stays null so future letter generation can still
        be blocked by the queryset filter (which keys on payment_status, not
        letter_voided_at).
        """
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("50.00"), payment_method="cheque"
        )
        # Donation has letter_status default ("pending"), no letter_batch.

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        donation.refresh_from_db()
        assert donation.letter_voided_at is None
        assert donation.letter_void_reason == ""
        assert donation.letter_status == "pending"

    def test_reversal_idempotent_on_letter_void(self) -> None:
        """FIN-BANK-REVERSAL-CASCADE-004: re-running does not stomp void timestamp.

        First reversal sets ``letter_voided_at`` to T1. Second reversal of
        the same slip finds zero rows at ``payment_status='completed'`` and
        is a no-op; the original T1 timestamp must survive a manual
        re-completion + re-reversal cycle (used for partial bounces) too.
        """
        slip = PayingInSlipFactory()
        donation = _make_completed_donation(
            slip, amount=Decimal("50.00"), payment_method="cheque"
        )
        printer = UserFactory()
        _attach_letter_batch(donation, status="sent", printer=printer)

        BankingService.reverse_donations_for_slip(slip, reason="First bounce")
        donation.refresh_from_db()
        first_voided_at = donation.letter_voided_at
        assert first_voided_at is not None

        # Manually flip back to completed so the second call has a target,
        # simulating an operator re-completing and bank re-rejecting.
        donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
        donation.save(update_fields=["payment_status"])

        BankingService.reverse_donations_for_slip(slip, reason="Second bounce")

        donation.refresh_from_db()
        # First-call timestamp wins; the second pass left it alone.
        assert donation.letter_voided_at == first_voided_at
        assert donation.letter_void_reason == "First bounce"


@pytest.mark.django_db(transaction=True)
class TestReverseDonationsCascadeNotifications:
    """FIN-BANK-REVERSAL-CASCADE-005 to 011: post-commit notification fan-out.

    Uses ``transaction=True`` so the ``transaction.on_commit`` hook scheduled
    by the cascade actually fires. Each test patches
    ``send_batch_status_email`` so we can assert email dispatch without
    hitting the real Celery task / Resend backend.
    """

    @staticmethod
    def _make_slip_with_processor(processor: Any) -> PayingInSlip:
        slip = PayingInSlipFactory(processed_by=processor)
        return slip

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_notifies_slip_processor(self, _mock_email: Any) -> None:
        """CASCADE-005: the operator who recorded the slip result is notified."""
        processor = UserFactory(email="processor@example.com")
        slip = self._make_slip_with_processor(processor)
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="cheque")

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        notifications = list(Notification.objects.filter(user=processor))
        assert len(notifications) == 1
        assert "Banking reversal" in notifications[0].title
        assert notifications[0].related_object_type == "PayingInSlip"

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_notifies_print_operator_when_letter_voided(
        self, _mock_email: Any
    ) -> None:
        """CASCADE-006: the print operator who issued the letter is notified.

        ``letter_batch.created_by`` is the most actionable recipient — they
        need to chase the donor / send a correction / hold further runs.
        Skipped when no letter was issued.
        """
        processor = UserFactory()
        printer = UserFactory(email="printer@example.com")
        slip = self._make_slip_with_processor(processor)
        donation = _make_completed_donation(
            slip, amount=Decimal("50.00"), payment_method="cheque"
        )
        _attach_letter_batch(donation, status="sent", printer=printer)

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        printer_notifs = list(Notification.objects.filter(user=printer))
        assert len(printer_notifs) == 1
        # Body should call out the voided-letter follow-up.
        assert "letter" in printer_notifs[0].message.lower()

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_notifies_qa_reviewer_and_creator_deduped(
        self, _mock_email: Any
    ) -> None:
        """CASCADE-007: batch.reviewed_by and batch.created_by are notified once.

        Two donations on the slip share the same batch (so the same reviewer
        + creator). The recipient set must dedupe so each user receives one
        notification per slip, not one per donation.
        """
        processor = UserFactory()
        reviewer = UserFactory(email="reviewer@example.com")
        creator = UserFactory(email="creator@example.com")
        slip = self._make_slip_with_processor(processor)
        campaign = CampaignFactory(client=slip.client, status="active")
        batch = DonationBatchFactory(
            campaign=campaign,
            created_by=creator,
            reviewed_by=reviewer,
            status="approved",
        )
        for _ in range(2):
            DonationFactory(
                campaign=campaign,
                batch=batch,
                amount=Decimal("25.00"),
                payment_method="cheque",
                payment_status=Donation.PAYMENT_STATUS_COMPLETED,
                qa_status="approved",
                paying_in_slip=slip,
            )

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        assert Notification.objects.filter(user=reviewer).count() == 1
        assert Notification.objects.filter(user=creator).count() == 1

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_card_donation_message_mentions_manual_refund(
        self, _mock_email: Any
    ) -> None:
        """CASCADE-008: card reversal surfaces the Stripe-refund manual hint.

        ``reverse_donations_for_slip`` does not auto-refund; the cascade
        just adds wording to the notification so ops know Stripe still
        holds the funds until they issue a refund manually.
        """
        processor = UserFactory()
        slip = self._make_slip_with_processor(processor)
        # Card donation completed (via QA card-capture flow), now reversed
        # by banking — that's the divergence case.
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="card")

        BankingService.reverse_donations_for_slip(slip, reason="Slip rejected")

        notif = Notification.objects.get(user=processor)
        assert "Stripe" in notif.message
        assert "refund" in notif.message.lower()
        # Severity escalates to ERROR for financial-drift cases.
        assert notif.notification_type == Notification.TYPE_ERROR

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_gift_aid_quiet_when_csv_not_submitted(
        self, _mock_email: Any
    ) -> None:
        """CASCADE-009: no HMRC-retraction line when CSV was never submitted.

        Regression guard for the alert-fatigue fix: ``gift_aid_report_path``
        alone (auto-populated for every gift-aid batch) must NOT trigger
        the retraction warning. Only ``gift_aid_submitted_at IS NOT NULL``
        (operator click) does.
        """
        processor = UserFactory()
        slip = self._make_slip_with_processor(processor)
        campaign = CampaignFactory(client=slip.client, status="active")
        # Path populated (auto behaviour), but never explicitly submitted.
        batch = DonationBatchFactory(
            campaign=campaign,
            status="approved",
            gift_aid_report_path="gift_aid_reports/test.csv",
            gift_aid_submitted_at=None,
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("50.00"),
            payment_method="cheque",
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status="approved",
            gift_aid=True,
            paying_in_slip=slip,
        )

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        notif = Notification.objects.get(user=processor)
        assert "HMRC" not in notif.message
        assert "retraction" not in notif.message.lower()

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_gift_aid_warns_when_csv_submitted(self, _mock_email: Any) -> None:
        """CASCADE-010: HMRC-retraction line fires when CSV was submitted."""
        processor = UserFactory()
        slip = self._make_slip_with_processor(processor)
        campaign = CampaignFactory(client=slip.client, status="active")
        batch = DonationBatchFactory(
            campaign=campaign,
            status="approved",
            gift_aid_report_path="gift_aid_reports/test.csv",
            gift_aid_submitted_at=timezone.now(),
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("50.00"),
            payment_method="cheque",
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status="approved",
            gift_aid=True,
            paying_in_slip=slip,
        )

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        notif = Notification.objects.get(user=processor)
        assert "HMRC" in notif.message
        # Severity is at least WARNING for HMRC-relevant impact.
        assert notif.notification_type in (
            Notification.TYPE_WARNING,
            Notification.TYPE_ERROR,
        )

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_html_body_escapes_reason(self, mock_email: Any) -> None:
        """CASCADE-SEC-001: free-text ``reason`` is HTML-escaped in the email body.

        The reversal reason is operator-supplied free text from the slip
        processing form (``custom_issue`` field). ``format_html`` does the
        escaping; this test guards against a regression to f-string
        concatenation.
        """
        processor = UserFactory(email="processor@example.com")
        slip = self._make_slip_with_processor(processor)
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="cheque")

        BankingService.reverse_donations_for_slip(
            slip, reason="<script>alert(1)</script>"
        )

        mock_email.delay.assert_called()
        html_message = mock_email.delay.call_args_list[0].kwargs["html_message"]
        assert "<script>alert(1)</script>" not in html_message
        # Escaped form must still be present so the operator can read what
        # was typed; this also confirms the value was not silently dropped.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_message

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_html_body_escapes_client_and_batch_names(
        self, mock_email: Any
    ) -> None:
        """CASCADE-SEC-002: ``client.name`` and ``batch.batch_name`` are HTML-escaped.

        Both values flow through staff-edited setup forms (Client setup,
        Batch creation). They use the same ``format_html`` codepath as
        ``reason`` so the escape is shared, but explicit coverage protects
        against a refactor that interpolates either value via an f-string.
        Setup needs ``gift_aid_submitted_at`` populated so the cascade
        emits the gift-aid retraction line, which is the only line that
        embeds ``batch.batch_name``.
        """
        processor = UserFactory(email="processor@example.com")
        malicious_client = ClientFactory(name="<img onerror=alert(2)>")
        slip = PayingInSlipFactory(processed_by=processor, client=malicious_client)
        campaign = CampaignFactory(client=malicious_client, status="active")
        batch = DonationBatchFactory(
            campaign=campaign,
            batch_name="<svg onload=alert(3)>",
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
            gift_aid_submitted_at=timezone.now(),
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("50.00"),
            payment_method="cheque",
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status="approved",
            gift_aid=True,
            paying_in_slip=slip,
        )

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        html_message = mock_email.delay.call_args_list[0].kwargs["html_message"]
        # Raw markup must NOT appear:
        assert "<img onerror=alert(2)>" not in html_message
        assert "<svg onload=alert(3)>" not in html_message
        # Escaped form must appear:
        assert "&lt;img onerror=alert(2)&gt;" in html_message
        assert "&lt;svg onload=alert(3)&gt;" in html_message

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_notification_link_set_to_slip_detail(
        self, _mock_email: Any
    ) -> None:
        """CASCADE-012: notifications carry a link to the slip detail page."""
        processor = UserFactory()
        slip = self._make_slip_with_processor(processor)
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="cheque")

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        notif = Notification.objects.get(user=processor)
        assert notif.link == reverse(
            "custom_admin:slip_detail", kwargs={"slip_id": slip.id}
        )

    @patch("core.tasks.send_batch_status_email")
    def test_reversal_dispatches_email_after_commit(self, mock_email: Any) -> None:
        """CASCADE-011: emails go via send_batch_status_email.delay post-commit.

        The mock target is ``banking.services.send_batch_status_email``
        (where the cascade imports it from), not ``core.tasks`` — Python
        import resolution happens at call time inside the helper so the
        local reference is what the mock replaces.
        """
        processor = UserFactory(email="processor@example.com")
        slip = self._make_slip_with_processor(processor)
        _make_completed_donation(slip, amount=Decimal("50.00"), payment_method="cheque")

        BankingService.reverse_donations_for_slip(slip, reason="Cheque bounce")

        mock_email.delay.assert_called()
        # Multiple recipients may exist (slip processor + batch reviewer +
        # batch creator). Assert the slip processor specifically received
        # an email; subject is identical across recipients.
        recipients = {
            call.kwargs["recipient_email"] for call in mock_email.delay.call_args_list
        }
        assert "processor@example.com" in recipients
        first_call = mock_email.delay.call_args_list[0]
        assert "Banking reversal" in first_call.kwargs["subject"]
