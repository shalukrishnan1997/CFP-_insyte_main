"""Daily banking dashboard and slip creation views.

The main dashboard lists donation batches that have items in the banking queue.
HTMX partials live in :mod:`banking.admin_views_htmx`.
Slip CRUD operations live in :mod:`banking.admin_views_slips`.
"""

import json

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from banking.models import PayingInSlip
from banking.services import SLIP_SETTLED_DONATION_Q, BankingService
from responsehandling.permissions import has_permission_or_is_staff

from .utils import parse_date_param
from .view_helpers import (
    build_banking_batch_detail_context,
    build_daily_banking_dashboard_context,
    build_slip_success_payload,
    fetch_slip_donations,
    parse_slip_creation_payload,
    resolve_slip_clients,
    resolve_slip_payment_type,
    validate_slip_creation_payload,
)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("view_donation")
def daily_banking_dashboard(request: HttpRequest) -> HttpResponse:
    """Daily banking dashboard: batches with items in the physical banking queue."""
    return render(
        request,
        "admin/daily_banking/dashboard.html",
        build_daily_banking_dashboard_context(request),
    )


@has_permission_or_is_staff("view_donation")
def daily_banking_batch_detail(request: HttpRequest, batch_id: int) -> HttpResponse:
    """Drill-down for one batch: eligible donations, transparency counts, slip action."""
    return render(
        request,
        "admin/daily_banking/batch_detail.html",
        build_banking_batch_detail_context(request, batch_id),
    )


# ---------------------------------------------------------------------------
# Slip creation
# ---------------------------------------------------------------------------


@has_permission_or_is_staff("add_donation")
@require_POST
def create_paying_in_slip(request: HttpRequest) -> HttpResponse:
    """Create paying-in slip(s) from selected batches."""
    try:
        payload = parse_slip_creation_payload(request)
        error = validate_slip_creation_payload(payload)
        if error:
            return JsonResponse({"success": False, "error": error}, status=400)

        banking_date = parse_date_param(payload.banking_date_str)

        with transaction.atomic():
            donations_qs = fetch_slip_donations(payload)
            if not donations_qs.exists():
                return JsonResponse(
                    {"success": False, "error": "No eligible donations found."},
                    status=400,
                )

            unsettled_ids = list(
                donations_qs.exclude(SLIP_SETTLED_DONATION_Q).values_list(
                    "id", flat=True
                )
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

            payment_type = resolve_slip_payment_type(donations_qs)
            clients = resolve_slip_clients(donations_qs)
            if len(clients) != 1:
                return JsonResponse(
                    {
                        "success": False,
                        "error": (
                            "Selected donations must belong to exactly one client."
                        ),
                    },
                    status=400,
                )
            primary_client_id = clients[0]["cid"] if clients else None

            slip = PayingInSlip.objects.create(
                slip_number=payload.slip_number,
                client_id=primary_client_id,
                payment_type=payment_type,
                banking_date=banking_date,
                notes=payload.notes,
                created_by=request.user,
                status="draft",
            )

            updated_count = donations_qs.update(paying_in_slip=slip)
            BankingService.recalculate_slip_totals(slip)

        return JsonResponse(build_slip_success_payload(slip, updated_count, clients))

    except json.JSONDecodeError, ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid data format."},
            status=400,
        )
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)
