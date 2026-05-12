"""Paying-in slip CRUD and management views.

All views operate on :model:`core.PayingInSlip` instances and are
guarded by permission decorators.
"""

import json
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from banking.models import PayingInSlip
from banking.services import (
    SLIP_REVERSAL_COMPLETION_STATUSES,
    SLIP_SETTLED_DONATION_Q,
    BankingService,
)
from clients.models import Client
from core.pagination import paginate_queryset
from donations.models import Donation
from responsehandling.permissions import has_permission_or_is_staff

from .utils import PAYMENT_TYPE_LABELS, parse_date_param

# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("view_donation")
def slip_list(request: HttpRequest) -> HttpResponse:
    """List all paying-in slips with filtering options.

    Args:
        request: HTTP request with optional GET filters.

    Returns:
        Rendered slip list page.
    """
    status_filter = request.GET.get("status", "")
    client_filter = request.GET.get("client", "")
    date_from_str = request.GET.get("date_from", "")
    date_to_str = request.GET.get("date_to", "")

    slips = PayingInSlip.objects.select_related(
        "client", "created_by", "banked_by"
    ).prefetch_related("donations")

    if status_filter:
        slips = slips.filter(status=status_filter)
    if client_filter:
        slips = slips.filter(client_id=client_filter)
    if date_from_str:
        slips = slips.filter(banking_date__gte=parse_date_param(date_from_str))
    if date_to_str:
        slips = slips.filter(banking_date__lte=parse_date_param(date_to_str))

    slips = slips.order_by("-banking_date", "-created_at")

    page_obj = paginate_queryset(slips, request, per_page=25)

    clients = Client.objects.filter(is_active=True).order_by("name")

    stats = PayingInSlip.objects.aggregate(
        total_slips=Count("id"),
        draft_count=Count("id", filter=Q(status="draft")),
        ready_count=Count("id", filter=Q(status="ready")),
        banked_count=Count("id", filter=Q(status="banked")),
        total_amount=Sum("total_amount"),
    )

    context = {
        "page_obj": page_obj,
        "clients": clients,
        "status_filter": status_filter,
        "client_filter": client_filter,
        "date_from": date_from_str,
        "date_to": date_to_str,
        "stats": stats,
        "status_choices": PayingInSlip.STATUS_CHOICES,
    }
    return render(request, "admin/daily_banking/slip_list.html", context)


@has_permission_or_is_staff("view_donation")
def slip_detail(request: HttpRequest, slip_id: int) -> HttpResponse:
    """View details of a specific paying-in slip.

    Args:
        request: HTTP request.
        slip_id: Primary key of the slip.

    Returns:
        Rendered slip detail page.
    """
    slip = get_object_or_404(
        PayingInSlip.objects.select_related("client", "created_by", "banked_by"),
        id=slip_id,
    )

    # Filter to settled donations only — pre-existing slips may still carry
    # pending-card donations whose totals were excluded from slip.total_amount
    # by recalculate_slip_totals; displaying them would mismatch the header.
    donations = (
        slip.donations.filter(SLIP_SETTLED_DONATION_Q)
        .select_related("campaign", "donor", "data_file_donor")
        .order_by("-donation_date", "-created_at")
    )

    context = {"slip": slip, "donations": donations}
    return render(request, "admin/daily_banking/slip_detail.html", context)


# ---------------------------------------------------------------------------
# Status / processing
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("change_donation")
@require_POST
def slip_update_status(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Update the status of a paying-in slip.

    Args:
        request: HTTP POST with ``status`` field.
        slip_id: Primary key of the slip.

    Returns:
        JSON response with new status.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)
    new_status = request.POST.get("status")

    if new_status not in dict(PayingInSlip.STATUS_CHOICES):
        return JsonResponse({"success": False, "error": "Invalid status"}, status=400)

    slip.status = new_status

    if new_status == "banked":
        slip.banked_at = timezone.now()
        slip.banked_by = request.user

    slip.save()

    return JsonResponse(
        {
            "success": True,
            "status": slip.status,
            "status_display": slip.get_status_display(),
        }
    )


@has_permission_or_is_staff("change_donation")
@require_POST
def slip_record_processing(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Record bank processing results for a paying-in slip.

    Args:
        request: HTTP POST with processing details.
        slip_id: Primary key of the slip.

    Returns:
        JSON response with updated slip data.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)

    if not slip.can_be_processed():
        return JsonResponse(
            {
                "success": False,
                "error": (
                    "Only slips with status 'ready' or 'submitted_to_bank' "
                    "can be processed"
                ),
            },
            status=400,
        )

    try:
        processed_amount = Decimal(request.POST.get("processed_amount", "0"))
    except (ValueError, TypeError):  # fmt: skip
        return JsonResponse(
            {"success": False, "error": "Invalid processed amount"}, status=400
        )

    completion_status = request.POST.get("completion_status")
    if completion_status not in dict(PayingInSlip.COMPLETION_STATUS_CHOICES):
        return JsonResponse(
            {"success": False, "error": "Invalid completion status"}, status=400
        )

    bank_processed_date_str = request.POST.get("bank_processed_date")
    try:
        bank_processed_date = parse_date_param(bank_processed_date_str)
    except (ValueError, TypeError):  # fmt: skip
        return JsonResponse(
            {"success": False, "error": "Invalid bank processed date"}, status=400
        )

    processing_issues_str = request.POST.get("processing_issues", "[]")
    try:
        processing_issues = json.loads(processing_issues_str)
        if not isinstance(processing_issues, list):
            processing_issues = []
    except json.JSONDecodeError:
        processing_issues = []

    custom_issue = request.POST.get("custom_issue", "").strip()

    if processed_amount > slip.total_amount:
        return JsonResponse(
            {
                "success": False,
                "error": (
                    f"Processed amount (£{processed_amount}) cannot exceed "
                    f"total amount (£{slip.total_amount})"
                ),
            },
            status=400,
        )

    # Cheque bounce / cash short / outright failure all leave linked
    # donations stranded at payment_status="completed" unless we explicitly
    # reverse them here. The reversal reason is composed from the operator's
    # custom_issue plus the structured issue codes so the audit trail captures
    # both the free text and the picklist selection.
    #
    # The reversal + slip-status update are wrapped in a single ``atomic``
    # block for two reasons:
    #   1. If ``mark_slip_as_processed`` raises, the donation reversals
    #      roll back so the slip and donation states stay in sync.
    #   2. ``BankingService.reverse_donations_for_slip`` schedules the
    #      reversal-cascade notification via ``transaction.on_commit``.
    #      Without this outer atomic, that callback would fire immediately
    #      after the inner reversal commits — i.e. before
    #      ``mark_slip_as_processed`` sets ``slip.processed_by`` — and the
    #      cascade would re-fetch the slip with ``processed_by=None``, so
    #      the operator who actually recorded the result would never be
    #      notified. Wrapping both calls keeps the on_commit hook deferred
    #      until processed_by has been written.
    reversed_count = 0
    with transaction.atomic():
        if completion_status in SLIP_REVERSAL_COMPLETION_STATUSES:
            reason_parts: list[str] = []
            if processing_issues:
                reason_parts.append(", ".join(str(code) for code in processing_issues))
            if custom_issue:
                reason_parts.append(custom_issue)
            reversal_reason = "; ".join(reason_parts) or completion_status
            reversed_count = BankingService.reverse_donations_for_slip(
                slip, reversal_reason
            )

        BankingService.mark_slip_as_processed(
            slip,
            processed_amount=processed_amount,
            completion_status=completion_status,
            bank_processed_date=bank_processed_date,
            processing_issues=processing_issues,
            custom_issue=custom_issue,
            processed_by=request.user,
        )

    return JsonResponse(
        {
            "success": True,
            "message": "Processing results recorded successfully",
            "slip": {
                "id": slip.id,
                "status": slip.status,
                "status_display": slip.get_status_display(),
                "processed_amount": str(slip.processed_amount),
                "unprocessed_amount": str(slip.get_unprocessed_amount()),
                "completion_status": slip.completion_status,
                "completion_status_display": slip.get_completion_status_display(),
            },
            "reversed_donation_count": reversed_count,
        }
    )


# ---------------------------------------------------------------------------
# Donation assignment
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("change_donation")
@require_POST
def slip_remove_donation(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Remove a donation from a paying-in slip.

    Args:
        request: HTTP POST with ``donation_id``.
        slip_id: Primary key of the slip.

    Returns:
        JSON response with updated totals.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)
    donation_id = request.POST.get("donation_id")

    if not donation_id:
        return JsonResponse(
            {"success": False, "error": "Donation ID required"}, status=400
        )

    if slip.status == "banked":
        return JsonResponse(
            {"success": False, "error": "Cannot modify banked slip"}, status=400
        )

    try:
        donation = Donation.objects.get(id=donation_id, paying_in_slip=slip)
        donation.paying_in_slip = None
        donation.save(update_fields=["paying_in_slip", "updated_at"])
        BankingService.recalculate_slip_totals(slip)

        return JsonResponse(
            {
                "success": True,
                "total_amount": str(slip.total_amount),
                "total_items": slip.total_items,
            }
        )
    except Donation.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Donation not found in slip"}, status=404
        )


@has_permission_or_is_staff("change_donation")
@require_POST
def slip_add_donations(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Add donations to an existing paying-in slip.

    Args:
        request: HTTP POST with ``donation_ids`` JSON array.
        slip_id: Primary key of the slip.

    Returns:
        JSON response with updated totals.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)

    if slip.status == "banked":
        return JsonResponse(
            {"success": False, "error": "Cannot modify banked slip"}, status=400
        )

    try:
        donation_ids = json.loads(request.POST.get("donation_ids", "[]"))
        if not donation_ids:
            return JsonResponse(
                {"success": False, "error": "No donations specified"}, status=400
            )

        donations = Donation.objects.filter(
            id__in=donation_ids,
            paying_in_slip__isnull=True,
        )

        if not donations.exists():
            return JsonResponse(
                {"success": False, "error": "No eligible donations found"},
                status=400,
            )

        unsettled_ids = list(
            donations.exclude(SLIP_SETTLED_DONATION_Q).values_list("id", flat=True)
        )
        if unsettled_ids:
            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        f"{len(unsettled_ids)} donation(s) are not settled "
                        "(card payment still pending or failed) and cannot "
                        "be added to a paying-in slip."
                    ),
                    "unsettled_donation_ids": [str(d) for d in unsettled_ids],
                },
                status=400,
            )

        selected_client_ids = list(
            donations.values_list("campaign__client_id", flat=True).distinct()
        )
        if len(selected_client_ids) != 1:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Selected donations must belong to exactly one client",
                },
                status=400,
            )

        selected_client_id = selected_client_ids[0]
        if slip.client_id and slip.client_id != selected_client_id:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Selected donations belong to a different client",
                },
                status=400,
            )

        if not slip.client_id:
            slip.client_id = selected_client_id
            slip.save(update_fields=["client", "updated_at"])

        updated_count = donations.update(paying_in_slip=slip)
        BankingService.recalculate_slip_totals(slip)

        return JsonResponse(
            {
                "success": True,
                "added_count": updated_count,
                "total_amount": str(slip.total_amount),
                "total_items": slip.total_items,
            }
        )
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid donation IDs format"}, status=400
        )


# ---------------------------------------------------------------------------
# Delete / print / edit
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("delete_donation")
@require_POST
def slip_delete(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Delete a paying-in slip (draft slips only).

    Args:
        request: HTTP POST request.
        slip_id: Primary key of the slip.

    Returns:
        JSON response confirming deletion.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)

    if slip.status != "draft":
        return JsonResponse(
            {"success": False, "error": "Only draft slips can be deleted"}, status=400
        )

    with transaction.atomic():
        # Detach every linked donation regardless of settled status — when a
        # draft slip is deleted we want all donations released back to the
        # banking pool, including any historical pending-card rows.
        slip.donations.update(paying_in_slip=None)
        slip_number = slip.slip_number
        slip.delete()

    return JsonResponse(
        {"success": True, "message": f"Slip {slip_number} deleted successfully"}
    )


@has_permission_or_is_staff("view_donation")
@require_GET
def slip_print(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Print view for a paying-in slip.

    Groups donations by payment method for the printed layout.

    Args:
        request: HTTP GET request.
        slip_id: Primary key of the slip.

    Returns:
        Rendered print-friendly page.
    """
    slip = get_object_or_404(
        PayingInSlip.objects.select_related("client", "created_by"), id=slip_id
    )

    # Filter to settled donations only so the printed slip cannot include
    # pending-card rows that were excluded from slip.total_amount by
    # recalculate_slip_totals.
    donations = (
        slip.donations.filter(SLIP_SETTLED_DONATION_Q)
        .select_related("campaign", "donor", "data_file_donor")
        .order_by("payment_method", "-amount")
    )

    grouped_donations: dict[str, list[Donation]] = {}
    for donation in donations:
        ptype = donation.payment_method
        if ptype not in grouped_donations:
            grouped_donations[ptype] = []
        grouped_donations[ptype].append(donation)

    context = {
        "slip": slip,
        "donations": donations,
        "grouped_donations": grouped_donations,
        "payment_type_labels": PAYMENT_TYPE_LABELS,
        "print_mode": True,
    }
    return render(request, "admin/daily_banking/slip_print.html", context)


@has_permission_or_is_staff("change_payinginslip")
def slip_edit(request: HttpRequest, slip_id: int) -> HttpResponse:
    """Edit an existing paying-in slip.

    Handles both the GET (form display) and POST (save) flows.

    Args:
        request: HTTP request.
        slip_id: Primary key of the slip.

    Returns:
        Rendered edit form or redirect to slip list.
    """
    slip = get_object_or_404(PayingInSlip, id=slip_id)

    if request.method == "POST":
        slip_number = request.POST.get("slip_number")
        banking_date_str = request.POST.get("banking_date")
        notes = request.POST.get("notes")

        if slip_number:
            slip.slip_number = slip_number

        if banking_date_str:
            try:
                slip.banking_date = datetime.strptime(
                    banking_date_str, "%Y-%m-%d"
                ).date()
            except ValueError:
                messages.error(request, "Invalid date format.")
                return render(
                    request, "admin/daily_banking/slip_edit.html", {"slip": slip}
                )

        slip.notes = notes
        slip.save()

        messages.success(request, f"Slip #{slip.id} updated successfully.")
        return redirect("custom_admin:slip_list")

    return render(request, "admin/daily_banking/slip_edit.html", {"slip": slip})
