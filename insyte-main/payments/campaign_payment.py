"""Campaign payment service for managing campaign-level payment processing.

Handles campaign payment eligibility checks and payment status tracking.
"""

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

if TYPE_CHECKING:
    from campaigns.models import Campaign

logger = logging.getLogger(__name__)


class CampaignPaymentService:
    """Service for campaign-level payment operations."""

    @staticmethod
    def check_payment_eligibility(campaign: Campaign) -> dict[str, Any]:
        """Check if campaign is eligible for payment processing.

        A campaign is eligible when:
        1. Has at least one batch
        2. All batches are QA approved
        3. Has donations with payment_method='card'

        Args:
            campaign: Campaign instance

        Returns:
            Dict with eligibility status and details
        """
        from donations.models import DonationBatch

        # Get all batches
        batches = campaign.donation_batches.all()
        total_batches = batches.count()

        if total_batches == 0:
            return {
                "eligible": False,
                "reason": "No donation batches in campaign",
                "total_batches": 0,
            }

        # Check if all batches are approved
        approved_batches = batches.filter(status=DonationBatch.STATUS_APPROVED).count()

        if approved_batches < total_batches:
            return {
                "eligible": False,
                "reason": f"Only {approved_batches}/{total_batches} batches are QA approved",
                "total_batches": total_batches,
                "approved_batches": approved_batches,
            }

        # Check for credit card donations
        credit_card_count = campaign.donations.filter(
            payment_method="card", donation_date__isnull=False
        ).count()

        if credit_card_count == 0:
            return {
                "eligible": False,
                "reason": "No credit card donations in campaign",
                "total_batches": total_batches,
                "approved_batches": approved_batches,
                "credit_card_count": 0,
            }

        # Campaign is eligible
        return {
            "eligible": True,
            "reason": "All batches approved and credit card donations present",
            "total_batches": total_batches,
            "approved_batches": approved_batches,
            "credit_card_count": credit_card_count,
        }

    @staticmethod
    def mark_campaign_eligible(campaign: Campaign) -> None:
        """Mark campaign as eligible for payment processing.

        Args:
            campaign: Campaign instance
        """
        eligibility = CampaignPaymentService.check_payment_eligibility(campaign)

        if eligibility["eligible"]:
            campaign.payment_eligible = True
            campaign.save(update_fields=["payment_eligible"])
            logger.info("Campaign %s marked as payment eligible", campaign.id)
        else:
            campaign.payment_eligible = False
            campaign.save(update_fields=["payment_eligible"])
            logger.info(
                "Campaign %s not eligible: %s", campaign.id, eligibility["reason"]
            )

    @staticmethod
    def get_eligible_campaigns():
        """Get all campaigns with payment processing activity or eligibility.

        Includes campaigns that are explicitly marked as eligible OR have
        batches that are ready for QA (pending_qa) or already processing.

        Returns:
            QuerySet of campaigns with annotations
        """
        from campaigns.models import Campaign
        from donations.models import DonationBatch

        # Include campaigns that:
        # 1. Are marked as payment_eligible (all batches approved)
        # 2. Have any batch with status 'pending_qa' (submitted for review)
        # 3. Have already started payment processing
        return (
            Campaign.objects.filter(
                Q(payment_eligible=True)
                | Q(donation_batches__status=DonationBatch.STATUS_PENDING_QA)
                | ~Q(payment_status="not_started")
            )
            .distinct()  # Avoid duplicates from donation_batches join
            .select_related("client", "created_by")
            .prefetch_related("donation_batches", "donations")
            .annotate(
                total_batches=Count("donation_batches", distinct=True),
                credit_card_count=Count(
                    "donations",
                    filter=Q(donations__payment_method="card"),
                    distinct=True,
                ),
                credit_card_amount=Sum(
                    "donations__amount",
                    filter=Q(donations__payment_method="card"),
                    default=0,
                ),
            )
            .order_by("-created_at")
        )

    @staticmethod
    def get_campaign_payment_summary(campaign: Campaign) -> dict[str, Any]:
        """Get payment processing summary for campaign.

        Args:
            campaign: Campaign instance

        Returns:
            Dict with payment statistics
        """
        # Get all batches
        batches = campaign.donation_batches.all()

        # Count credit card donations by status
        donations = campaign.donations.filter(payment_method="card")

        total_donations = donations.count()
        total_amount = donations.aggregate(total=Sum("amount"))["total"] or 0

        # Count by payment status
        pending = donations.filter(payment_status="pending").count()
        processing = donations.filter(payment_status="processing").count()
        completed = donations.filter(payment_status="completed").count()
        failed = donations.filter(payment_status="failed").count()

        return {
            "campaign_id": str(campaign.id),
            "campaign_name": campaign.name,
            "total_batches": batches.count(),
            "total_donations": total_donations,
            "total_amount": float(total_amount),
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "payment_status": campaign.payment_status,
            "payment_eligible": campaign.payment_eligible,
        }

    @staticmethod
    def update_campaign_payment_status(campaign: Campaign) -> None:
        """Update campaign payment status based on batch statuses.

        Args:
            campaign: Campaign instance
        """
        batches = campaign.donation_batches.all()

        if not batches.exists():
            return

        # Count batch statuses
        completed = batches.filter(payment_status="completed").count()
        processing = batches.filter(payment_status="processing").count()
        failed = batches.filter(payment_status="failed").count()
        total = batches.count()

        # Determine overall status
        if completed == total:
            status = "completed"
        elif processing > 0:
            status = "in_progress"
        elif completed > 0 and completed < total:
            status = "partially_completed"
        elif failed == total:
            status = "failed"
        else:
            status = "not_started"

        # Update campaign
        campaign.payment_status = status

        # Count successful/failed payments
        donations = campaign.donations.filter(payment_method="card")
        campaign.successful_payments = donations.filter(
            payment_status="completed"
        ).count()
        campaign.failed_payments = donations.filter(payment_status="failed").count()
        campaign.total_payment_amount = (
            donations.filter(payment_status="completed").aggregate(total=Sum("amount"))[
                "total"
            ]
            or 0
        )

        if status == "completed":
            campaign.payment_processed_at = timezone.now()

        campaign.save()

        logger.info("Updated campaign %s payment status to %s", campaign.id, status)

    @staticmethod
    def auto_check_campaigns_eligibility() -> int:
        """Auto-check all campaigns for payment eligibility.

        Returns:
            Number of campaigns marked as eligible
        """
        from campaigns.models import Campaign

        campaigns = Campaign.objects.filter(
            status=Campaign.STATUS_ACTIVE, payment_eligible=False
        )

        eligible_count = 0

        for campaign in campaigns:
            eligibility = CampaignPaymentService.check_payment_eligibility(campaign)
            if eligibility["eligible"]:
                campaign.payment_eligible = True
                campaign.save(update_fields=["payment_eligible"])
                eligible_count += 1

        logger.info(
            f"Auto-checked {campaigns.count()} campaigns, {eligible_count} marked eligible"
        )

        return eligible_count
