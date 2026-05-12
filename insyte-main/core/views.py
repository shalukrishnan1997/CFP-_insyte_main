import logging
import time
import uuid

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render

from campaigns.models import Campaign
from donations.models import Donation
from responsehandling.permissions import is_authenticated_and_is_staff

from .agent_debug import agent_debug_log
from .constants import CURRENCY_CODE, CURRENCY_SYMBOL

logger = logging.getLogger(__name__)
User = get_user_model()


@is_authenticated_and_is_staff
def user_dashboard(request: HttpRequest) -> HttpResponse:
    """User dashboard based on permissions.

    Args:
        request: HTTP request from an authenticated user with system access.

    Returns:
        Rendered dashboard page with campaign/donation summaries.
    """
    total_start = time.perf_counter()
    user = request.user

    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "dashboard view entered",
        "H2",
        {
            "username": getattr(user, "username", "?"),
            "is_staff": getattr(user, "is_staff", False),
        },
    )
    # #endregion

    # Get user's accessible campaigns
    campaign_phase = time.perf_counter()
    can_view_campaign = user.has_perm("campaigns.view_campaign")
    my_campaigns = (
        Campaign.objects.filter(created_by=user).count() if can_view_campaign else 0
    )
    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "campaign metrics resolved",
        "H2",
        {
            "elapsed_ms": round((time.perf_counter() - campaign_phase) * 1000, 2),
            "can_view_campaign": can_view_campaign,
            "my_campaigns": my_campaigns,
        },
    )
    # #endregion

    donation_phase = time.perf_counter()
    can_view_donation = user.has_perm("donations.view_donation")
    my_donations = (
        Donation.objects.filter(filled_by=user).count() if can_view_donation else 0
    )
    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "donation metrics resolved",
        "H3",
        {
            "elapsed_ms": round((time.perf_counter() - donation_phase) * 1000, 2),
            "can_view_donation": can_view_donation,
            "my_donations": my_donations,
        },
    )
    # #endregion

    context: dict = {
        "user": user,
        "my_campaigns": my_campaigns,
        "my_donations": my_donations,
        "currency_symbol": CURRENCY_SYMBOL,
        "currency_code": CURRENCY_CODE,
    }

    recent_campaigns_phase = time.perf_counter()
    if can_view_campaign:
        context["recent_campaigns"] = (
            Campaign.objects.filter(created_by=user)
            .select_related("client")
            .order_by("-created_at")[:5]
        )
    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "recent campaigns prepared",
        "H3",
        {
            "elapsed_ms": round(
                (time.perf_counter() - recent_campaigns_phase) * 1000, 2
            ),
            "included": "recent_campaigns" in context,
        },
    )
    # #endregion

    recent_donations_phase = time.perf_counter()
    if can_view_donation:
        context["recent_donations"] = (
            Donation.objects.filter(batch__created_by=user)
            .select_related("campaign", "donor")
            .order_by("-created_at")[:10]
        )
    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "recent donations prepared",
        "H4",
        {
            "elapsed_ms": round(
                (time.perf_counter() - recent_donations_phase) * 1000, 2
            ),
            "included": "recent_donations" in context,
        },
    )
    # #endregion

    # #region agent log
    agent_debug_log(
        "core/views.py:user_dashboard",
        "before render",
        "H5",
        {
            "total_elapsed_ms": round((time.perf_counter() - total_start) * 1000, 2),
        },
    )
    # #endregion
    return render(request, "admin/dashboard.html", context)


# HTMX Views for Dynamic Content
@is_authenticated_and_is_staff
def htmx_campaign_search(request: HttpRequest) -> JsonResponse:
    """HTMX endpoint for campaign search.

    Args:
        request: HTTP request with GET parameter ``q`` for the search term.

    Returns:
        JSON list of matching campaigns (id, title).
    """
    query = request.GET.get("q", "")
    campaigns = Campaign.objects.filter(name__icontains=query)[:10]

    results = [{"id": str(c.id), "title": c.name} for c in campaigns]
    return JsonResponse({"campaigns": results})


@is_authenticated_and_is_staff
def htmx_donation_stats(request: HttpRequest, campaign_id: uuid.UUID) -> JsonResponse:
    """HTMX endpoint for real-time donation statistics.

    Args:
        request: HTTP request.
        campaign_id: UUID of the campaign.

    Returns:
        JSON with total_donations, total_amount, and currency info.
    """
    campaign = get_object_or_404(Campaign, id=campaign_id)
    stats = Donation.objects.filter(campaign=campaign).aggregate(
        total_amount=Sum("amount"),
    )

    return JsonResponse(
        {
            "total_donations": Donation.objects.filter(campaign=campaign).count(),
            "total_amount": float(stats["total_amount"] or 0),
            "currency_symbol": CURRENCY_SYMBOL,
            "currency_code": CURRENCY_CODE,
        }
    )


@is_authenticated_and_is_staff
def htmx_validate_email(request: HttpRequest) -> JsonResponse:
    """HTMX endpoint to validate email in real-time.

    Args:
        request: HTTP request with GET parameter ``email``.

    Returns:
        JSON with valid (bool) and message.
    """
    email = request.GET.get("email", "").strip()

    if not email:
        return JsonResponse({"valid": False, "message": "Email is required"})

    # Simple email validation
    if "@" not in email or "." not in email:
        return JsonResponse({"valid": False, "message": "Invalid email format"})

    # Check if email exists
    if User.objects.filter(email=email).exists():
        return JsonResponse({"valid": False, "message": "Email already exists"})

    return JsonResponse({"valid": True, "message": "Email is available"})
