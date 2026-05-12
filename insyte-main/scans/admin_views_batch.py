"""Donation batch maintenance views for the scan-first intake workflow.

All donations now originate from OCR scan processing. This module only retains
the delete action for empty batches.
"""

import uuid

from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from donations.models import DonationBatch
from responsehandling.permissions import has_permission_or_is_staff


@require_POST
@has_permission_or_is_staff("delete_donationbatch")
def donation_batch_delete(
    request: HttpRequest, client_id: uuid.UUID, campaign_id: uuid.UUID, batch_id: int
) -> JsonResponse:
    """Delete an empty donation batch.

    Validates that:
    - Batch exists and belongs to the specified campaign/client
    - Batch is empty (has 0 donations)
    - Client is active

    Returns JSON response with success/error status.
    """
    try:
        # Get the batch with related objects in one query
        batch = get_object_or_404(
            DonationBatch.objects.select_related("campaign__client"),
            pk=batch_id,
            campaign__pk=campaign_id,
            campaign__client__pk=client_id,
            campaign__client__is_active=True,
        )

        # Check if batch is empty
        donation_count = batch.donations.count()
        if donation_count > 0 or (batch.total_donations and batch.total_donations > 0):
            return JsonResponse(
                {
                    "success": False,
                    "error": "Cannot delete batch with donations. Batch must be empty.",
                },
                status=400,
            )

        # Check if batch is approved - prevent deletion of approved batches
        if batch.status == "approved":
            return JsonResponse(
                {"success": False, "error": "Cannot delete an approved batch."},
                status=400,
            )

        # Store batch name for success message
        batch_name = batch.batch_name

        # Delete the batch
        batch.delete()

        return JsonResponse(
            {"success": True, "message": f"Batch '{batch_name}' deleted successfully."}
        )

    except Exception as e:
        return JsonResponse(
            {"success": False, "error": f"Error deleting batch: {e!s}"}, status=500
        )
