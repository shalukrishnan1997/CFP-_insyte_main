"""Regression tests for the campaign-metrics dashboard cache.

The campaign-management page caches "X Total / Y Active / Z Draft" badge
counts under ``CAMPAIGN_METRICS_CACHE_KEY``. Before the fix, the entry
was never invalidated, so a newly created draft campaign wouldn't appear
in the dashboard for up to 5 minutes (TTL). The signals registered in
``CampaignsConfig.ready`` invalidate the cache on every Campaign save /
delete.
"""

import pytest
from django.core.cache import cache

from campaigns.cache_keys import CAMPAIGN_METRICS_CACHE_KEY
from tests.factories import CampaignFactory


@pytest.mark.django_db()
class TestCampaignMetricsCacheInvalidation:
    def setup_method(self) -> None:
        cache.delete(CAMPAIGN_METRICS_CACHE_KEY)

    def test_cache_cleared_on_create(self) -> None:
        """Saving a Campaign drops the metrics cache entry."""
        cache.set(
            CAMPAIGN_METRICS_CACHE_KEY,
            {"live_count": 99, "draft_count": 99, "total_campaigns": 99},
            300,
        )
        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is not None

        CampaignFactory(status="draft")

        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is None

    def test_cache_cleared_on_update(self) -> None:
        """Updating a Campaign also drops the cache (status flips count)."""
        campaign = CampaignFactory(status="draft")
        cache.set(
            CAMPAIGN_METRICS_CACHE_KEY,
            {"live_count": 0, "draft_count": 1, "total_campaigns": 1},
            300,
        )
        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is not None

        campaign.status = "active"
        campaign.save()

        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is None

    def test_cache_cleared_on_delete(self) -> None:
        """Deleting a Campaign drops the cache."""
        campaign = CampaignFactory(status="draft")
        cache.set(
            CAMPAIGN_METRICS_CACHE_KEY,
            {"live_count": 0, "draft_count": 1, "total_campaigns": 1},
            300,
        )
        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is not None

        campaign.delete()

        assert cache.get(CAMPAIGN_METRICS_CACHE_KEY) is None
