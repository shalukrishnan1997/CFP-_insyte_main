"""Invoice metrics calculation service.

Computes billing-period metrics (donation counts, gift aid, campaigns, etc.)
used by both the invoice creation workflow and the metrics API endpoint.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Q, Sum

from audit.models import AuditLog
from campaigns.models import Campaign
from clients.models import Client
from core.date_utils import parse_date
from donations.models import Donation, DonationBatch
from donors.models import Donor
from letters.models import LetterTemplate

logger = logging.getLogger(__name__)


class InvoiceMetricsService:
    """Calculates comprehensive metrics for invoice billing periods."""

    @staticmethod
    def calculate(
        client: Client,
        start_date: str,
        end_date: str,
        campaign: Campaign | None = None,
    ) -> dict[str, Any]:
        """Calculate comprehensive metrics for an invoice billing period.

        Supports both client-wide and campaign-specific invoice generation.

        Args:
            client: Client object.
            start_date: Start date (ISO ``YYYY-MM-DD`` or UK ``DD/MM/YYYY``).
            end_date: End date (ISO ``YYYY-MM-DD`` or UK ``DD/MM/YYYY``).
            campaign: Optional campaign to narrow the scope.

        Returns:
            Dictionary of metric key→value pairs matching the ``Invoice``
            model fields.

        Raises:
            ValueError: If dates cannot be parsed.
        """
        start = _parse_flexible_date(start_date)
        end = _parse_flexible_date(end_date)

        if start is None or end is None:
            msg = "Invalid date format. Expected YYYY-MM-DD or DD/MM/YYYY."
            raise ValueError(msg)

        # --- Donation aggregates ---
        donations_qs = _donation_queryset(client, campaign, start, end)
        total_donations = donations_qs.count()
        total_donation_amount = donations_qs.aggregate(total=Sum("amount"))[
            "total"
        ] or Decimal("0")

        gift_aid_qs = donations_qs.filter(gift_aid=True)
        gift_aid_count = gift_aid_qs.count()
        gift_aid_amount = gift_aid_qs.aggregate(total=Sum("amount"))[
            "total"
        ] or Decimal("0")

        # --- HGV / LGV ---
        if campaign:
            hgv_threshold = campaign.hgv_amount or Decimal("1000")
            lgv_threshold = campaign.lgv_amount or Decimal("50")
        else:
            hgv_threshold = Decimal("1000")
            lgv_threshold = Decimal("50")

        hgv_count = donations_qs.filter(amount__gte=hgv_threshold).count()
        lgv_count = donations_qs.filter(amount__lt=lgv_threshold).count()

        # --- Campaigns ---
        campaigns_created, campaigns_updated = _campaign_metrics(
            client, campaign, start, end
        )

        # --- Letters & templates ---
        letters_generated = _letters_generated(campaign, start, end)
        templates_created = _templates_created(client, campaign, start, end)

        # --- Donors ---
        donors_added = Donor.objects.filter(
            created_at__date__gte=start, created_at__date__lte=end
        ).count()
        donors_updated = AuditLog.objects.filter(
            model_name="Donor",
            action="UPDATE",
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()

        # --- Batch operations ---
        batch_filter: dict[str, object] = {
            "created_at__date__gte": start,
            "created_at__date__lte": end,
        }
        if campaign:
            batch_filter["campaign"] = campaign
        else:
            batch_filter["campaign__client"] = client
        batch_operations = DonationBatch.objects.filter(**batch_filter).count()

        # --- Misc audit counts ---
        data_files = _data_files_processed(campaign, start, end)
        reports_generated = AuditLog.objects.filter(
            model_name="Report",
            action__in=["CREATE", "EXPORT"],
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
        users_managed = AuditLog.objects.filter(
            model_name="User",
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
        api_calls = AuditLog.objects.filter(
            action="READ",
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
        notifications_sent = AuditLog.objects.filter(
            summary__icontains="notification",
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()

        return {
            "total_donations_captured": total_donations,
            "total_donation_amount": total_donation_amount,
            "gift_aid_captured": gift_aid_count,
            "gift_aid_amount": gift_aid_amount,
            "letters_generated": letters_generated,
            "campaigns_created": campaigns_created,
            "campaigns_updated": campaigns_updated,
            "hgv_identified": hgv_count,
            "lgv_identified": lgv_count,
            "donor_responses_received": 0,  # placeholder
            "donor_notifications_sent": notifications_sent,
            "data_files_processed": data_files,
            "batch_operations_completed": batch_operations,
            "reports_generated": reports_generated,
            "templates_created": templates_created,
            "users_managed": users_managed,
            "donors_added": donors_added,
            "donors_updated": donors_updated,
            "api_calls_made": api_calls,
            "storage_used_mb": Decimal("0.00"),  # placeholder
        }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_flexible_date(value: str) -> datetime | None:
    """Parse a date string in ISO (YYYY-MM-DD) or UK (DD/MM/YYYY) format.

    Args:
        value: Date string.

    Returns:
        ``date`` object or ``None`` if parsing fails.
    """
    return parse_date(value)  # type: ignore[return-value]


def _donation_queryset(
    client: Client,
    campaign: Campaign | None,
    start: object,
    end: object,
) -> object:
    """Return a filtered Donation queryset for the billing period."""
    if campaign:
        return Donation.objects.filter(
            campaign=campaign,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
    return Donation.objects.filter(
        campaign__client=client,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )


def _campaign_metrics(
    client: Client,
    campaign: Campaign | None,
    start: object,
    end: object,
) -> tuple[int, int]:
    """Return (campaigns_created, campaigns_updated) counts."""
    if campaign:
        created = (
            1
            if (
                campaign.created_at.date() >= start
                and campaign.created_at.date() <= end
            )
            else 0
        )
        updated = AuditLog.objects.filter(
            model_name="Campaign",
            action="UPDATE",
            object_id=str(campaign.id),
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
    else:
        created = Campaign.objects.filter(
            client=client,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
        updated = (
            AuditLog.objects.filter(
                model_name="Campaign",
                action="UPDATE",
                created_at__date__gte=start,
                created_at__date__lte=end,
            )
            .filter(Q(summary__icontains=client.name))
            .count()
        )
    return created, updated


def _letters_generated(campaign: Campaign | None, start: object, end: object) -> int:
    """Return count of letters generated in the billing period."""
    base = AuditLog.objects.filter(
        model_name="GeneratedLetter",
        action="CREATE",
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    if campaign:
        base = base.filter(summary__icontains=campaign.name)
    return base.count()


def _templates_created(
    client: Client,
    campaign: Campaign | None,
    start: object,
    end: object,
) -> int:
    """Return count of letter templates created in the billing period."""
    if campaign:
        return LetterTemplate.objects.filter(
            campaign=campaign,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).count()
    return LetterTemplate.objects.filter(
        campaign__client=client,
        created_at__date__gte=start,
        created_at__date__lte=end,
    ).count()


def _data_files_processed(campaign: Campaign | None, start: object, end: object) -> int:
    """Return count of data files processed in the billing period."""
    base = AuditLog.objects.filter(
        model_name="CampaignDataFile",
        action="CREATE",
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    if campaign:
        base = base.filter(summary__icontains=campaign.name)
    return base.count()
