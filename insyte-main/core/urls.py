from django.urls import path

from . import views
from .webhooks import scan_upload_webhook, scanner_campaigns_list, stripe_webhook

app_name = "core"

urlpatterns = [
    # User Dashboard
    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    # HTMX Endpoints
    path(
        "htmx/campaigns/search/",
        views.htmx_campaign_search,
        name="htmx_campaign_search",
    ),
    path(
        "htmx/campaigns/<uuid:campaign_id>/stats/",
        views.htmx_donation_stats,
        name="htmx_donation_stats",
    ),
    path("htmx/validate-email/", views.htmx_validate_email, name="htmx_validate_email"),
    # Webhooks
    path("webhooks/stripe/", stripe_webhook, name="stripe_webhook"),
    path("webhooks/scan-upload/", scan_upload_webhook, name="scan_upload_webhook"),
    # Scanner workstation API (HMAC-authenticated, read-only)
    path(
        "webhooks/scanner/campaigns/",
        scanner_campaigns_list,
        name="scanner_campaigns_list",
    ),
]
