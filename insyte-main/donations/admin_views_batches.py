"""Donation batch list and detail views.

All batches are created automatically by the OCR scan processing pipeline.
This module only exposes list and detail pages for scan-created batches.
"""

import uuid

from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from campaigns.models import Campaign
from clients.models import Client
from core.pagination import paginate_queryset
from donations.models import DonationBatch
from responsehandling.permissions import has_permission_or_is_staff


@has_permission_or_is_staff("view_donationbatch")
def donation_batch_list(
    request: HttpRequest, client_id: uuid.UUID, campaign_id: uuid.UUID
) -> HttpResponse:
    """List all donation batches for a campaign.

    Batches are created exclusively via the OCR scan processing pipeline.
    Supports filtering by QA status and pagination.

    Args:
        request: HTTP request.
        client_id: UUID of the client.
        campaign_id: UUID of the campaign.

    Returns:
        Rendered batch list page.
    """
    client = get_object_or_404(Client, pk=client_id, is_active=True)
    campaign = get_object_or_404(Campaign, pk=campaign_id, client=client)

    status_filter = request.GET.get("status", "pending_qa")
    batches_qs = (
        DonationBatch.objects.filter(campaign=campaign)
        .select_related("created_by")
        .annotate(donation_count=Count("donations"))
    )
    if status_filter and status_filter != "all":
        batches_qs = batches_qs.filter(
            status__in=[s.strip() for s in status_filter.split(",")]
        )

    batches = paginate_queryset(
        batches_qs.order_by("-created_at"), request, per_page_param="per_page"
    )

    context = {
        "client": client,
        "campaign": campaign,
        "batches": batches,
        "active": "donations",
        "status_filter": status_filter,
    }
    return render(request, "admin/donation_batch_list.html", context)


@has_permission_or_is_staff("view_donationbatch")
def donation_batch_detail(
    request: HttpRequest, client_id: uuid.UUID, campaign_id: uuid.UUID, batch_id: int
) -> HttpResponse:
    """Show details of a specific donation batch.

    Args:
        request: HTTP request.
        client_id: UUID of the client.
        campaign_id: UUID of the campaign.
        batch_id: Integer PK of the batch.

    Returns:
        Rendered batch detail page.
    """
    client = get_object_or_404(Client, pk=client_id, is_active=True)
    campaign = get_object_or_404(Campaign, pk=campaign_id, client=client)
    batch = get_object_or_404(DonationBatch, pk=batch_id, campaign=campaign)

    donations = paginate_queryset(
        batch.donations.select_related("donor", "data_file_donor").order_by(
            "created_at"
        ),
        request,
        per_page_param="per_page",
    )

    context = {
        "client": client,
        "campaign": campaign,
        "batch": batch,
        "donations": donations,
        "active": "donations",
    }
    return render(request, "admin/donation_batch_detail.html", context)
