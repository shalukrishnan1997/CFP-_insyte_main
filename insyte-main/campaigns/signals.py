"""Signal handlers for the campaigns app.

The campaign-management dashboard caches aggregate counts (live / draft /
total) keyed under ``campaigns.cache_keys.CAMPAIGN_METRICS_CACHE_KEY``.
Without invalidation, a newly created draft campaign would not appear in
the dashboard's "X Total / Y Active / Z Draft" badges until the cache TTL
expired (up to 5 minutes). ``invalidate_campaign_metrics`` below drops
the entry on every Campaign save / delete so the dashboard reflects
writes immediately. Wire it up via ``connect_campaign_signals()`` from
``CampaignsConfig.ready``.
"""

from typing import Any

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save

from campaigns.cache_keys import CAMPAIGN_METRICS_CACHE_KEY


def invalidate_campaign_metrics(sender: Any, **kwargs: Any) -> None:
    """Drop the cached metrics so the dashboard re-reads on next request."""
    cache.delete(CAMPAIGN_METRICS_CACHE_KEY)


def connect_campaign_signals() -> None:
    """Register Campaign post_save / post_delete receivers."""
    from campaigns.models import Campaign

    post_save.connect(
        invalidate_campaign_metrics,
        sender=Campaign,
        dispatch_uid="campaigns.invalidate_metrics_post_save",
    )
    post_delete.connect(
        invalidate_campaign_metrics,
        sender=Campaign,
        dispatch_uid="campaigns.invalidate_metrics_post_delete",
    )
