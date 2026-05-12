"""Shared cache-key constants for the campaigns app.

The keys live in their own module so that ``signals.py`` (which is imported
during app setup, *before* ``admin_views.py`` is reachable) can reference
them without dragging the heavier admin_views module into the import graph.
"""

CAMPAIGN_METRICS_CACHE_KEY = "campaign_metrics_v4"
