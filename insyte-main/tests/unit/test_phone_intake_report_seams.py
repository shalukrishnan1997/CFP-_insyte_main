"""Report-side regression seams for the phone-intake auto-approval flow.

The auto-approval change shifts when ``payment_status=completed`` and
``qa_status=approved`` flip for phone card donations (intake time vs.
QA-approval time). Several downstream report consumers filter on those
fields. The plan's audit concluded none of them need code changes, but
these tests pin the behaviour so a future filter tweak can't silently
break the contract.

Covered seams:
  1. Banking ``SLIP_SETTLED_DONATION_Q`` — phone card donation captured
     mid-day is eligible for a same-day paying-in slip total.
  2. HMRC Gift Aid CSV — auto-approved phone donations flow through
     ``_run_gift_aid_for_batch`` when ``apply_phone_intake_auto_approval``
     closes the batch on a clean charge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from banking.services import BankingService
from donations.intake import apply_phone_intake_auto_approval
from donations.models import Donation, DonationBatch
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    PayingInSlipFactory,
)


@pytest.mark.django_db()
class TestBankingSlipSameDayEligibility:
    """Phone card donation captured at 06:00 must aggregate into a slip
    rendered later the same day.

    Pre-change: phone cards sat at ``payment_status=requires_capture``
    until QA approval (typically end-of-day). Post-change: they hit
    ``completed`` immediately. ``SLIP_SETTLED_DONATION_Q`` only counts
    cards with ``payment_status=completed`` — so the slip total now
    grows in real time. The math must stay correct.
    """

    def test_completed_phone_card_aggregates_into_slip(self) -> None:
        slip = PayingInSlipFactory(payment_type="card")
        campaign = CampaignFactory(client=slip.client)
        batch = DonationBatchFactory(campaign=campaign)
        # Phone card donation auto-approved at intake — landed at completed.
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            paying_in_slip=slip,
            amount=Decimal("75.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            field_data={"intake_method": "phone"},
        )

        BankingService.recalculate_slip_totals(slip)

        slip.refresh_from_db()
        assert slip.total_amount == Decimal("75.00")
        assert slip.total_items == 1
        # Sanity: the donation reference matches.
        assert slip.donations.filter(pk=donation.pk).exists()

    def test_requires_capture_phone_card_excluded_from_slip(self) -> None:
        """Pending-review-donor card path still defers capture — those
        donations should not inflate the slip total until QA approves.
        """
        slip = PayingInSlipFactory(payment_type="card")
        campaign = CampaignFactory(client=slip.client)
        batch = DonationBatchFactory(campaign=campaign)
        DonationFactory(
            campaign=campaign,
            batch=batch,
            paying_in_slip=slip,
            amount=Decimal("75.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_REQUIRES_CAPTURE,
            qa_status=Donation.QA_STATUS_FLAGGED,
            field_data={"intake_method": "phone"},
        )

        BankingService.recalculate_slip_totals(slip)

        slip.refresh_from_db()
        assert slip.total_amount == Decimal("0.00")
        assert slip.total_items == 0

    def test_mixed_phone_card_and_non_card_aggregate_correctly(self) -> None:
        """Phone card (completed) + cheque (no payment_status gate) on the
        same slip both count, and the total reflects both."""
        slip = PayingInSlipFactory(payment_type="mixed")
        campaign = CampaignFactory(client=slip.client)
        batch = DonationBatchFactory(campaign=campaign)
        DonationFactory(
            campaign=campaign,
            batch=batch,
            paying_in_slip=slip,
            amount=Decimal("50.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            field_data={"intake_method": "phone"},
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            paying_in_slip=slip,
            amount=Decimal("25.00"),
            payment_method=Donation.PAYMENT_METHOD_CHEQUE,
            payment_status=Donation.PAYMENT_STATUS_PENDING,
            qa_status=Donation.QA_STATUS_APPROVED,
            field_data={"intake_method": "phone"},
        )

        BankingService.recalculate_slip_totals(slip)

        slip.refresh_from_db()
        assert slip.total_amount == Decimal("75.00")
        assert slip.total_items == 2


@pytest.mark.django_db()
class TestGiftAidCsvFromAutoApprovedBatch:
    """HMRC Gift Aid CSV picks up auto-approved phone donations.

    The CSV builder filters on ``qa_status=approved AND gift_aid=true``
    and runs whenever ``DonationBatch.status`` flips to approved. With
    one batch per call, a clean card charge synchronously approves both
    the donation and its parent batch — Gift Aid then sees the donation
    via ``_run_gift_aid_for_batch``. This test pins that contract.
    """

    def _phone_donation(
        self, *, gift_aid: bool, qa_status: str = Donation.QA_STATUS_PENDING
    ) -> tuple[Donation, DonationBatch]:
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_PENDING_QA,
        )
        donor = DonorFactory(client=batch.campaign.client)
        donation = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            donor=donor,
            data_file_donor=None,
            system_donor=None,
            amount=Decimal("100.00"),
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=qa_status,
            gift_aid=gift_aid,
            field_data={"intake_method": "phone"},
        )
        return donation, batch

    def test_gift_aid_csv_includes_auto_approved_phone_donation(self) -> None:
        from core.tasks import _run_gift_aid_for_batch

        donation, batch = self._phone_donation(gift_aid=True)

        apply_phone_intake_auto_approval(
            donation, note="Auto-approved at phone intake — card captured live."
        )

        batch.refresh_from_db()
        donation.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        assert donation.qa_status == Donation.QA_STATUS_APPROVED

        gift_aid_result = _run_gift_aid_for_batch(batch)
        assert gift_aid_result["success"] is True
        assert gift_aid_result["row_count"] == 1

    def test_gift_aid_csv_omits_non_gift_aid_phone_donation(self) -> None:
        """Donor opted out of Gift Aid — donation excluded from the CSV."""
        from core.tasks import _run_gift_aid_for_batch

        donation, batch = self._phone_donation(gift_aid=False)
        apply_phone_intake_auto_approval(donation, note="Auto-approved.")
        batch.refresh_from_db()

        gift_aid_result = _run_gift_aid_for_batch(batch)
        assert gift_aid_result.get("row_count", 0) == 0
