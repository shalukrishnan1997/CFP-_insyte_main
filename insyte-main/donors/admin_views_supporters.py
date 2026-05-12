"""Supporter (SystemDonor) lookup views for staff admin panel.

Provides list and detail views for browsing, searching, and filtering
permanent system-of-record donor profiles. Includes HTMX partial for
paginated donation history.
"""

import uuid

from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from core.constants import CURRENCY_SYMBOL
from core.pagination import paginate_queryset
from donations.models import Donation
from donors.models import SystemDonor
from responsehandling.permissions import is_authenticated_and_is_staff


@is_authenticated_and_is_staff
def supporter_list(request: HttpRequest) -> HttpResponse:
    """Searchable, filterable list of all supporters (SystemDonor profiles).

    Supports searching by external URN, name, email, postcode and filtering
    by contact status. Results are paginated.

    Args:
        request: HTTP request with optional GET parameters:
            search: Free-text search (external_urn, name, email, postcode).
            status: Filter by contact_status value.
            page: Page number for pagination.
            per_page: Items per page (default 25).

    Returns:
        Rendered supporter list page.
    """
    queryset = SystemDonor.objects.select_related("created_by", "client").order_by(
        "last_name", "first_name"
    )

    search_query = request.GET.get("search", "").strip()
    if search_query:
        queryset = queryset.filter(
            Q(external_urn__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(postcode__icontains=search_query)
        )

    status_filter = request.GET.get("status", "")
    if status_filter:
        queryset = queryset.filter(contact_status=status_filter)

    queryset = queryset.annotate(
        total_donated=Sum("donations__amount"),
        donation_count=Count("donations"),
    )

    total_count = queryset.count()
    page_obj = paginate_queryset(
        queryset, request, per_page=25, per_page_param="per_page"
    )

    context = {
        "donors": page_obj,
        "total_count": total_count,
        "search_query": search_query,
        "status_filter": status_filter,
        "status_choices": SystemDonor.CONTACT_STATUS_CHOICES,
        "currency_symbol": CURRENCY_SYMBOL,
        "active": "supporters",
        "breadcrumbs": [{"name": "Supporters"}],
    }
    return render(request, "admin/supporters/list.html", context)


@is_authenticated_and_is_staff
def supporter_detail(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Detail page for a single supporter (SystemDonor).

    Shows full profile, consent/GDPR flags, and paginated donation
    history with campaign and amount information.

    Args:
        request: HTTP request.
        pk: SystemDonor UUID primary key.

    Returns:
        Rendered supporter detail page.
    """
    donor = get_object_or_404(SystemDonor, pk=pk)

    donations_qs = (
        Donation.objects.filter(system_donor=donor)
        .select_related("campaign", "campaign__client", "batch")
        .order_by("-donation_date", "-created_at")
    )

    donations_page = paginate_queryset(donations_qs, request, per_page=20)

    stats = donations_qs.aggregate(
        total_donated=Sum("amount"),
        donation_count=Count("id"),
    )

    context = {
        "donor": donor,
        "donations": donations_page,
        "total_donated": stats["total_donated"] or 0,
        "donation_count": stats["donation_count"] or 0,
        "currency_symbol": CURRENCY_SYMBOL,
        "active": "supporters",
        "breadcrumbs": [
            {
                "name": "Supporters",
                "url": reverse("custom_admin:supporter_list"),
            },
            {"name": donor.full_name},
        ],
    }
    return render(request, "admin/supporters/detail.html", context)


@is_authenticated_and_is_staff
@require_GET
def htmx_supporter_donations(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """HTMX partial: paginated donation table for a supporter.

    Returns only the donation table partial for use with hx-get
    pagination on the supporter detail page.

    Args:
        request: HTTP request with page GET parameter.
        pk: SystemDonor UUID primary key.

    Returns:
        Rendered partial template with paginated donations.
    """
    donor = get_object_or_404(SystemDonor, pk=pk)

    donations_qs = (
        Donation.objects.filter(system_donor=donor)
        .select_related("campaign", "campaign__client", "batch")
        .order_by("-donation_date", "-created_at")
    )

    donations_page = paginate_queryset(donations_qs, request, per_page=20)

    context = {
        "donor": donor,
        "donations": donations_page,
        "currency_symbol": CURRENCY_SYMBOL,
    }
    return render(request, "admin/supporters/partials/_donation_table.html", context)
