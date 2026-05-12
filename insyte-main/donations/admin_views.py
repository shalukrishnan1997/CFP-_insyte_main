"""Donation edit view.

All donations are created automatically by the OCR scan processing pipeline.
This module only provides editing of OCR-created donations during QA review.
"""

import uuid
from datetime import date

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from donations.models import Donation
from responsehandling.permissions import has_permission_or_is_staff


def _maybe_parse_date(raw: str) -> date | None:
    """Parse a date string; return None if empty or invalid.

    Args:
        raw: Raw date string from POST data.

    Returns:
        Parsed date or None.
    """
    return parse_date(raw) if raw else None


def _validate_custom_fields(
    request: HttpRequest, fields: list
) -> tuple[dict[str, str], list[str]]:
    """Extract and validate custom field values from POST data.

    Args:
        request: HTTP request with POST data.
        fields: Campaign custom field objects.

    Returns:
        Tuple of (field_data dict, list of error strings).
    """
    custom_fields: dict[str, str] = {}
    errors: list[str] = []
    for field in fields:
        value = request.POST.get(f"field_{field.id}", "").strip()
        if field.required and not value:
            errors.append(f"{field.label} is required.")
        custom_fields[str(field.id)] = value
    return custom_fields, errors


def _save_donation_from_post(
    request: HttpRequest,
    donation: Donation,
    fields: list,
) -> list[str]:
    """Validate and save donation data from POST.

    Args:
        request: HTTP request with POST data.
        donation: Donation instance to update.
        fields: Campaign custom field list.

    Returns:
        List of validation error strings (empty on success).
    """
    custom_fields, errors = _validate_custom_fields(request, fields)

    if errors:
        for field in fields:
            field.initial = request.POST.get(f"field_{field.id}", "").strip()
        return errors

    donation.field_data = custom_fields
    donation.payment_method = request.POST.get("payment_method", "").strip() or None
    donation.gift_aid = request.POST.get("gift_aid") == "on"
    donation.donation_frequency = (
        request.POST.get("donation_frequency", "").strip() or None
    )
    donation.cheque_number = request.POST.get("cheque_no", "").strip()

    donation.donation_date = timezone.now().date()

    parsed_cheque_date = _maybe_parse_date(request.POST.get("cheque_date", "").strip())
    if parsed_cheque_date:
        donation.cheque_date = parsed_cheque_date

    donation.save()
    return []


def _build_donation_edit_context(
    donation: Donation,
    fields: list,
) -> dict:
    """Build the template context for the donation edit view.

    Args:
        donation: Donation instance being edited.
        fields: Campaign custom field list with `initial` values pre-populated.

    Returns:
        Template context dictionary.
    """
    campaign = donation.campaign
    batch = donation.batch
    client = campaign.client
    return {
        "campaign": campaign,
        "batch": batch,
        "fields": fields,
        "donation": donation,
        "active": "donations",
        "editing": True,
        "breadcrumbs": [
            {
                "name": campaign.name,
                "url": reverse(
                    "custom_admin:donation_batch_list",
                    kwargs={"client_id": client.id, "campaign_id": campaign.id},
                ),
            },
            {
                "name": f"Batch #{batch.id}",
                "url": reverse(
                    "custom_admin:donation_batch_detail",
                    kwargs={
                        "client_id": client.id,
                        "campaign_id": campaign.id,
                        "batch_id": batch.id,
                    },
                ),
            },
            {"name": f"Edit Donation #{donation.id}", "url": None},
        ],
    }


@has_permission_or_is_staff("change_donation")
def donation_edit(request: HttpRequest, donation_id: uuid.UUID) -> HttpResponse:
    """Edit an existing OCR-created donation during QA review.

    Args:
        request: HTTP request.
        donation_id: Primary key of the donation to edit.

    Returns:
        Rendered edit form on GET/validation error, redirect to batch detail on success.
    """
    donation = get_object_or_404(
        Donation.objects.select_related("batch", "campaign__client"), pk=donation_id
    )
    campaign = donation.campaign
    batch = donation.batch
    client = campaign.client
    fields = list(campaign.fields.filter(is_default_field=False).order_by("order"))

    for f in fields:
        f.initial = donation.field_data.get(str(f.id), "")

    if request.method == "POST":
        errors = _save_donation_from_post(request, donation, fields)
        if errors:
            for err in errors:
                messages.error(request, err)
        else:
            messages.success(request, "Donation updated successfully.")
            return redirect(
                "custom_admin:donation_batch_detail",
                client_id=client.id,
                campaign_id=campaign.id,
                batch_id=batch.id,
            )

    return render(
        request,
        "admin/donation_edit.html",
        _build_donation_edit_context(donation, fields),
    )
