"""Supporter helpers for client portal views."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from django.db.models import Count, Q, Sum
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse

from client_portal.helpers.common import get_client_campaigns
from clients.models import Client
from core.constants import CURRENCY_SYMBOL
from core.pagination import paginate_queryset
from donations.models import Donation
from donors.models import DataFileDonor, Donor


@dataclass(frozen=True)
class PortalSupporterFilters:
    """Normalized filter values for client portal supporter views."""

    search_query: str
    campaign_id: str


def build_portal_supporter_filters(request: HttpRequest) -> PortalSupporterFilters:
    """Normalize request parameters for the supporters list view."""
    return PortalSupporterFilters(
        search_query=request.GET.get("search", "").strip(),
        campaign_id=request.GET.get("campaign", ""),
    )


def build_supporter_scope_filter(client: Client, campaign_id: str) -> Q:
    """Return the donation scope filter used across supporter views."""
    scope_filter = Q(donations__campaign__client=client)
    if campaign_id:
        scope_filter &= Q(donations__campaign_id=campaign_id)
    return scope_filter


def apply_supporter_search(queryset: Any, search_query: str) -> Any:
    """Apply the standard supporter search fields to a donor queryset."""
    if not search_query:
        return queryset

    return queryset.filter(
        Q(urn__icontains=search_query)
        | Q(first_name__icontains=search_query)
        | Q(last_name__icontains=search_query)
        | Q(email__icontains=search_query)
        | Q(postcode__icontains=search_query)
    )


def get_scoped_supporters(
    model: Any,
    *,
    client: Client,
    filters: PortalSupporterFilters,
) -> Any:
    """Return supporter records with totals scoped to the active client."""
    scope_filter = build_supporter_scope_filter(client, filters.campaign_id)
    queryset = model.objects.filter(scope_filter).distinct()
    queryset = apply_supporter_search(queryset, filters.search_query)
    return queryset.annotate(
        total_donated=Sum("donations__amount", filter=scope_filter),
        donation_count=Count("donations", filter=scope_filter),
    )


def build_supporter_list_item(
    supporter: Any,
    *,
    detail_view_name: str,
    detail_arg: object,
) -> SimpleNamespace:
    """Serialize one supporter row for the portal list template."""
    return SimpleNamespace(
        urn=supporter.urn,
        full_name=supporter.full_name,
        email=getattr(supporter, "email", "") or "",
        postcode=getattr(supporter, "postcode", "") or "",
        total_donated=supporter.total_donated,
        donation_count=supporter.donation_count,
        detail_url=reverse("client_portal:" + detail_view_name, args=[detail_arg]),
    )


def get_supporter_list_items(
    client: Client,
    filters: PortalSupporterFilters,
) -> list[SimpleNamespace]:
    """Return merged and sorted house-file + data-file supporter rows."""
    house_supporters = [
        build_supporter_list_item(
            supporter,
            detail_view_name="client_supporter_detail",
            detail_arg=supporter.urn,
        )
        for supporter in get_scoped_supporters(
            Donor,
            client=client,
            filters=filters,
        )
    ]
    data_file_supporters = [
        build_supporter_list_item(
            supporter,
            detail_view_name="client_supporter_detail_data_file",
            detail_arg=supporter.pk,
        )
        for supporter in get_scoped_supporters(
            DataFileDonor,
            client=client,
            filters=filters,
        )
    ]

    return sorted(
        house_supporters + data_file_supporters,
        key=lambda supporter: supporter.full_name.lower(),
    )


def build_supporter_list_context(
    request: HttpRequest,
    *,
    client: Client,
    filters: PortalSupporterFilters,
) -> dict[str, Any]:
    """Build template context for the client portal supporters list."""
    supporters = get_supporter_list_items(client, filters)
    donors_page = paginate_queryset(supporters, request, per_page=25)
    return {
        "client": client,
        "donors": donors_page,
        "total_count": len(supporters),
        "search_query": filters.search_query,
        "campaign_id": filters.campaign_id,
        "all_campaigns": get_client_campaigns(client),
        "currency_symbol": CURRENCY_SYMBOL,
        "active": "supporters",
    }


def get_scoped_supporter_or_404(
    client: Client,
    *,
    model: Any,
    lookup_field: str,
    lookup_value: object,
) -> Any:
    """Return a supporter only if they have donations under the given client."""
    queryset = model.objects.filter(donations__campaign__client=client).distinct()
    return get_object_or_404(queryset, **{lookup_field: lookup_value})


def get_supporter_donations(
    client: Client,
    *,
    donation_field: str,
    supporter: Any,
) -> Any:
    """Return donations scoped to one supporter and the active client."""
    return (
        Donation.objects.filter(campaign__client=client, **{donation_field: supporter})
        .select_related("campaign", "scan_placeholder")
        .order_by("-donation_date", "-created_at")
    )


def build_supporter_detail_context(
    request: HttpRequest,
    *,
    client: Client,
    donor: Any,
    donations_qs: Any,
) -> dict[str, Any]:
    """Build template context for either supporter detail route."""
    donations_page = paginate_queryset(donations_qs, request, per_page=20)
    stats = donations_qs.aggregate(
        total_donated=Sum("amount"),
        donation_count=Count("id"),
    )
    return {
        "client": client,
        "donor": donor,
        "donations": donations_page,
        "total_donated": stats["total_donated"] or 0,
        "donation_count": stats["donation_count"] or 0,
        "currency_symbol": CURRENCY_SYMBOL,
        "active": "supporters",
    }
