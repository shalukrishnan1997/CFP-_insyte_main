"""Dashboard helpers for client portal views."""

from typing import Any

from django.core.cache import cache
from django.db.models import Avg, Count, Sum

from campaigns.models import Campaign
from clients.models import Client
from core.constants import CURRENCY_CODE, CURRENCY_SYMBOL
from donations.models import Donation


def build_client_dashboard_context(client: Client) -> dict[str, Any]:
    """Build cached dashboard context for the active portal client."""
    cache_key = f"client_dashboard_{client.id}"
    cached_context = cache.get(cache_key)
    if cached_context is not None:
        return {**cached_context, "active": "dashboard"}

    campaigns = list(
        Campaign.objects.filter(client=client)
        .only("id", "name", "status", "created_at")
        .order_by("-created_at")
    )
    donation_stats = Donation.objects.filter(campaign__client=client).aggregate(
        total_donations=Count("id"),
        total_amount=Sum("amount"),
        avg_donation=Avg("amount"),
    )
    top_campaigns = list(
        Donation.objects.filter(campaign__client=client)
        .values("campaign__name")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")[:5]
    )
    base_context = {
        "client": client,
        "campaigns": campaigns,
        "total_campaigns": len(campaigns),
        "active_campaigns": sum(
            campaign.status == Campaign.STATUS_ACTIVE for campaign in campaigns
        ),
        "total_donations": donation_stats["total_donations"] or 0,
        "total_amount": donation_stats["total_amount"] or 0,
        "avg_donation": donation_stats["avg_donation"] or 0,
        "top_campaigns": top_campaigns,
        "currency_symbol": CURRENCY_SYMBOL,
        "currency_code": CURRENCY_CODE,
    }
    cache.set(cache_key, base_context, 300)
    return {**base_context, "active": "dashboard"}
