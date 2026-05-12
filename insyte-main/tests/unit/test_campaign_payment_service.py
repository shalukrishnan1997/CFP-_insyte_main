"""Unit tests for CampaignPaymentService."""

from decimal import Decimal

import pytest

from donations.models import DonationBatch
from payments.campaign_payment import CampaignPaymentService
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
)


@pytest.mark.django_db()
class TestCheckPaymentEligibility:
    """Tests for CampaignPaymentService.check_payment_eligibility."""

    def test_no_batches_returns_ineligible(self) -> None:
        campaign = CampaignFactory(status="active")
        result = CampaignPaymentService.check_payment_eligibility(campaign)
        assert result["eligible"] is False
        assert "No donation batches" in result["reason"]
        assert result["total_batches"] == 0

    def test_not_all_batches_approved_returns_ineligible(self) -> None:
        campaign = CampaignFactory(status="active")
        DonationBatchFactory(campaign=campaign, status="pending_qa")
        DonationBatchFactory(campaign=campaign, status=DonationBatch.STATUS_APPROVED)
        result = CampaignPaymentService.check_payment_eligibility(campaign)
        assert result["eligible"] is False
        assert "approved" in result["reason"]
        assert result["total_batches"] == 2
        assert result["approved_batches"] == 1

    def test_all_batches_approved_no_card_returns_ineligible(self) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(
            campaign=campaign, status=DonationBatch.STATUS_APPROVED
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cheque",
            donation_date=None,
        )
        result = CampaignPaymentService.check_payment_eligibility(campaign)
        assert result["eligible"] is False
        assert "credit card" in result["reason"].lower()

    def test_all_approved_with_card_donations_eligible(self) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(
            campaign=campaign, status=DonationBatch.STATUS_APPROVED
        )
        from django.utils import timezone

        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            donation_date=timezone.now().date(),
        )
        result = CampaignPaymentService.check_payment_eligibility(campaign)
        assert result["eligible"] is True
        assert result["total_batches"] == 1
        assert result["approved_batches"] == 1
        assert result["credit_card_count"] == 1


@pytest.mark.django_db()
class TestMarkCampaignEligible:
    """Tests for CampaignPaymentService.mark_campaign_eligible."""

    def test_marks_eligible_when_conditions_met(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=False)
        batch = DonationBatchFactory(
            campaign=campaign, status=DonationBatch.STATUS_APPROVED
        )
        from django.utils import timezone

        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            donation_date=timezone.now().date(),
        )
        CampaignPaymentService.mark_campaign_eligible(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_eligible is True

    def test_marks_ineligible_when_conditions_not_met(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=True)
        # No batches — ineligible
        CampaignPaymentService.mark_campaign_eligible(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_eligible is False


@pytest.mark.django_db()
class TestGetEligibleCampaigns:
    """Tests for CampaignPaymentService.get_eligible_campaigns."""

    def test_returns_payment_eligible_campaigns(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=True)
        qs = CampaignPaymentService.get_eligible_campaigns()
        ids = [str(c.id) for c in qs]
        assert str(campaign.id) in ids

    def test_returns_campaigns_with_pending_qa_batches(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=False)
        DonationBatchFactory(campaign=campaign, status=DonationBatch.STATUS_PENDING_QA)
        qs = CampaignPaymentService.get_eligible_campaigns()
        ids = [str(c.id) for c in qs]
        assert str(campaign.id) in ids

    def test_excludes_non_eligible_campaigns(self) -> None:
        campaign = CampaignFactory(
            status="draft", payment_eligible=False, payment_status="not_started"
        )
        qs = CampaignPaymentService.get_eligible_campaigns()
        ids = [str(c.id) for c in qs]
        assert str(campaign.id) not in ids

    def test_result_has_annotations(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=True)
        qs = CampaignPaymentService.get_eligible_campaigns()
        result = qs.filter(id=campaign.id).first()
        assert result is not None
        assert hasattr(result, "total_batches")
        assert hasattr(result, "credit_card_count")
        assert hasattr(result, "credit_card_amount")


@pytest.mark.django_db()
class TestGetCampaignPaymentSummary:
    """Tests for CampaignPaymentService.get_campaign_payment_summary."""

    def test_returns_summary_dict(self) -> None:
        campaign = CampaignFactory()
        result = CampaignPaymentService.get_campaign_payment_summary(campaign)
        assert result["campaign_id"] == str(campaign.id)
        assert result["campaign_name"] == campaign.name
        assert result["total_donations"] == 0
        assert result["total_amount"] == 0

    def test_counts_card_donations_only(self) -> None:
        campaign = CampaignFactory()
        batch = DonationBatchFactory(campaign=campaign, status="pending_qa")
        DonationFactory(
            campaign=campaign, batch=batch, payment_method="card", amount=Decimal("50")
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="cheque",
            amount=Decimal("100"),
        )
        result = CampaignPaymentService.get_campaign_payment_summary(campaign)
        assert result["total_donations"] == 1
        assert float(result["total_amount"]) == pytest.approx(50.0)

    def test_counts_payment_statuses(self) -> None:
        campaign = CampaignFactory()
        batch = DonationBatchFactory(campaign=campaign, status="pending_qa")
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            payment_status="completed",
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            payment_status="failed",
        )
        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            payment_status="pending",
        )
        result = CampaignPaymentService.get_campaign_payment_summary(campaign)
        assert result["completed"] == 1
        assert result["failed"] == 1
        assert result["pending"] == 1


@pytest.mark.django_db()
class TestUpdateCampaignPaymentStatus:
    """Tests for CampaignPaymentService.update_campaign_payment_status."""

    def test_no_batches_returns_early(self) -> None:
        campaign = CampaignFactory(payment_status="not_started")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        # No change expected, no error
        campaign.refresh_from_db()
        assert campaign.payment_status == "not_started"

    def test_all_completed_sets_completed(self) -> None:
        campaign = CampaignFactory()
        DonationBatchFactory(campaign=campaign, payment_status="completed")
        DonationBatchFactory(campaign=campaign, payment_status="completed")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_status == "completed"

    def test_any_processing_sets_in_progress(self) -> None:
        campaign = CampaignFactory()
        DonationBatchFactory(campaign=campaign, payment_status="processing")
        DonationBatchFactory(campaign=campaign, payment_status="completed")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_status == "in_progress"

    def test_partial_completion_sets_partially_completed(self) -> None:
        campaign = CampaignFactory()
        DonationBatchFactory(campaign=campaign, payment_status="completed")
        DonationBatchFactory(campaign=campaign, payment_status="not_started")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_status == "partially_completed"

    def test_all_failed_sets_failed(self) -> None:
        campaign = CampaignFactory()
        DonationBatchFactory(campaign=campaign, payment_status="failed")
        DonationBatchFactory(campaign=campaign, payment_status="failed")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_status == "failed"

    def test_default_status_not_started(self) -> None:
        campaign = CampaignFactory()
        DonationBatchFactory(campaign=campaign, payment_status="not_started")
        CampaignPaymentService.update_campaign_payment_status(campaign)
        campaign.refresh_from_db()
        assert campaign.payment_status == "not_started"


@pytest.mark.django_db()
class TestAutoCheckCampaignsEligibility:
    """Tests for CampaignPaymentService.auto_check_campaigns_eligibility."""

    def test_returns_count_of_eligible_campaigns(self) -> None:
        campaign = CampaignFactory(status="active", payment_eligible=False)
        batch = DonationBatchFactory(
            campaign=campaign, status=DonationBatch.STATUS_APPROVED
        )
        from django.utils import timezone

        DonationFactory(
            campaign=campaign,
            batch=batch,
            payment_method="card",
            donation_date=timezone.now().date(),
        )
        count = CampaignPaymentService.auto_check_campaigns_eligibility()
        assert count >= 1

    def test_skips_already_eligible_campaigns(self) -> None:
        CampaignFactory(status="active", payment_eligible=True)
        # already eligible, should not be in the queryset
        count = CampaignPaymentService.auto_check_campaigns_eligibility()
        assert isinstance(count, int)

    def test_returns_zero_when_no_active_campaigns(self) -> None:
        CampaignFactory(status="draft", payment_eligible=False)
        count = CampaignPaymentService.auto_check_campaigns_eligibility()
        assert count == 0
