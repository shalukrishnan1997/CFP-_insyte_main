"""Shared client portal helper functions used across portal views."""

from typing import Any

from django.core.cache import cache
from django.http import HttpRequest

from campaigns.models import Campaign
from clients.models import Client


def get_portal_client(request: HttpRequest) -> Client:
    """Return the active client for the authenticated portal user."""
    return request.user.client_portal_profile.client


def get_client_campaigns(
    client: Client,
    *,
    cache_timeout: int = 600,
) -> list[Campaign]:
    """Return the active client's campaigns ordered for portal filters."""
    cache_key = f"client_campaigns_{client.id}"
    campaigns = cache.get_or_set(
        cache_key,
        lambda: list(
            Campaign.objects.filter(client=client).only("id", "name").order_by("name")
        ),
        cache_timeout,
    )
    return list(campaigns or [])


def build_client_report_categories() -> list[dict[str, Any]]:
    """Return grouped report metadata for the reports landing page."""
    from custom_admin.views.reports.constants import REPORT_CATEGORIES, REPORT_TYPES

    return [
        {
            **category,
            "report_list": [
                {"key": key, **REPORT_TYPES[key]}
                for key in category["reports"]
                if key in REPORT_TYPES
            ],
        }
        for category in REPORT_CATEGORIES
    ]


def get_client_report_config(report_type: str) -> dict[str, Any] | None:
    """Return the report configuration for a portal report type."""
    from custom_admin.views.reports.constants import REPORT_TYPES

    return REPORT_TYPES.get(report_type)


def campaign_belongs_to_client(client: Client, campaign_id: str) -> bool:
    """Return whether the given campaign belongs to the client."""
    return Campaign.objects.filter(id=campaign_id, client=client).exists()


def get_client_campaign(client: Client, campaign_id: str) -> Campaign | None:
    """Return the owned campaign instance, if present."""
    if not campaign_id:
        return None
    return Campaign.objects.filter(id=campaign_id, client=client).only("name").first()
