"""Shared helper functions for daily banking views."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from django.db.models import (
    Count,
    DecimalField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

from banking.models import PayingInSlip
from campaigns.models import Campaign
from clients.models import Client
from core.pagination import paginate_queryset
from donations.models import Donation, DonationBatch

from .utils import (
    BANKABLE_PAYMENT_TYPES,
    PAYMENT_TYPE_LABELS,
    apply_donation_filters,
    base_unassigned_core_filter,
    base_unassigned_filter,
    donation_banking_date_window_q,
    get_date_range,
    parse_date_param,
)

# NOTE: qa_utils import is deferred inside _donation_slip_preview_dict()
# to break a circular import (custom_admin.views.__init__ re-exports from
# banking.admin_views, which transitively loads this module).

# Modal preview list cap (slip still includes every eligible donation).
SLIP_MODAL_PREVIEW_MAX = 30


@dataclass(frozen=True)
class BankingFilterState:
    """Active filter values used by daily banking views."""

    client_id: str | None
    campaign_id: str | None
    payment_method: str | None
    batch_id: str | None


@dataclass(frozen=True)
class SlipCreationPayload:
    """Normalized request payload for paying-in-slip creation."""

    slip_number: str
    banking_date_str: str | None
    notes: str
    batch_ids: list[Any]
    donation_ids: list[Any]
    date_from_str: str | None = None
    date_to_str: str | None = None


def _donation_slip_preview_dict(donation: Donation) -> dict[str, str]:
    """Serialize donation fields for paying-in slip modal preview (JSON-safe)."""
    from custom_admin.views.qa_utils import get_donation_display_urn

    urn = get_donation_display_urn(donation)
    if donation.system_donor_id:
        supporter = f"{donation.system_donor.first_name} {donation.system_donor.last_name}".strip()
    elif donation.donor_id:
        supporter = f"{donation.donor.first_name} {donation.donor.last_name}".strip()
    elif donation.data_file_donor_id:
        supporter = (
            f"{donation.data_file_donor.first_name} "
            f"{donation.data_file_donor.last_name}".strip()
        )
    else:
        supporter = donation.donor_name or "Unknown"
    ref = ""
    pm = donation.payment_method
    if pm == "cheque":
        ref = donation.cheque_number or ""
    elif pm == "postal_order":
        ref = donation.postal_order_number or ""
    elif pm == "caf":
        ref = donation.caf_voucher_number or ""
    return {
        "id": str(donation.pk),
        "supporterName": supporter,
        "paymentTypeLabel": donation.get_payment_method_display(),
        "amount": str(donation.amount),
        "reference": ref,
        "urn": urn,
    }


def get_banking_filter_state(request: HttpRequest) -> BankingFilterState:
    """Return the active dashboard and HTMX filter values."""
    return BankingFilterState(
        client_id=request.GET.get("client_id"),
        campaign_id=request.GET.get("campaign_id"),
        payment_method=request.GET.get("payment_method"),
        batch_id=request.GET.get("batch_id"),
    )


def build_banking_batches_queryset(request: HttpRequest) -> Any:
    """Batches that have at least one donation in the banking queue for the date range."""
    _banking_date, date_from, date_to = get_date_range(request)
    filters = get_banking_filter_state(request)
    banking_q = base_unassigned_filter(date_from, date_to)
    eligible_exists = Exists(
        Donation.objects.filter(banking_q, batch_id=OuterRef("pk"))
    )
    count_subquery = Subquery(
        Donation.objects.filter(banking_q, batch_id=OuterRef("pk"))
        .values("batch_id")
        .annotate(_c=Count("id"))
        .values("_c")[:1],
        output_field=IntegerField(),
    )
    sum_subquery = Subquery(
        Donation.objects.filter(banking_q, batch_id=OuterRef("pk"))
        .values("batch_id")
        .annotate(_s=Sum("amount"))
        .values("_s")[:1],
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    qs = (
        DonationBatch.objects.filter(eligible_exists)
        .select_related("campaign", "campaign__client")
        .annotate(
            unbanked_bankable_count=Coalesce(count_subquery, Value(0)),
            unbanked_bankable_total=Coalesce(
                sum_subquery,
                Value(
                    Decimal("0"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            ),
        )
    )
    if filters.client_id:
        qs = qs.filter(campaign__client_id=filters.client_id)
    if filters.campaign_id:
        qs = qs.filter(campaign_id=filters.campaign_id)
    if filters.batch_id:
        qs = qs.filter(pk=filters.batch_id)
    if filters.payment_method:
        pm = filters.payment_method
        qs = qs.filter(
            Exists(
                Donation.objects.filter(
                    banking_q,
                    batch_id=OuterRef("pk"),
                    payment_method=pm,
                )
            )
        )
    return qs.order_by(F("reviewed_at").desc(nulls_last=True), "-created_at")


def build_daily_banking_dashboard_context(request: HttpRequest) -> dict[str, object]:
    """Build the dashboard context for the batch-first daily banking view."""
    banking_date, date_from, date_to = get_date_range(request)
    filters = get_banking_filter_state(request)
    base_q = base_unassigned_filter(date_from, date_to)

    batches_qs = build_banking_batches_queryset(request)
    total_banking_batches = batches_qs.count()
    page_obj = paginate_queryset(batches_qs, request, per_page=25)

    donations_qs = apply_donation_filters(
        Donation.objects.filter(base_q),
        client_id=filters.client_id,
        campaign_id=filters.campaign_id,
        payment_method=filters.payment_method,
        batch_id=filters.batch_id,
    )
    dashboard_donations_qs = (
        donations_qs.select_related(
            "campaign",
            "campaign__client",
            "batch",
            "donor",
            "data_file_donor",
        )
        .only(
            "id",
            "amount",
            "donation_date",
            "created_at",
            "payment_method",
            "cheque_number",
            "postal_order_number",
            "caf_voucher_number",
            "campaign__name",
            "campaign__client__name",
            "batch__batch_name",
            "batch__id",
            "donor__first_name",
            "donor__last_name",
            "donor__urn",
            "data_file_donor__first_name",
            "data_file_donor__last_name",
            "data_file_donor__urn",
        )
        .order_by("-donation_date", "-created_at", "id")
    )
    donations_page_obj = paginate_queryset(
        dashboard_donations_qs,
        request,
        per_page=50,
        page_param="donations_page",
    )

    totals = donations_qs.aggregate(
        total_unassigned=Count("id"), total_amount=Sum("amount")
    )
    client_count = (
        donations_qs.values("campaign__client__id").distinct().count()
        if totals["total_unassigned"]
        else 0
    )

    base_donations = Donation.objects.filter(base_q)
    available_clients = list(
        Client.objects.filter(campaigns__donations__in=base_donations)
        .distinct()
        .order_by("name")
    )
    available_campaigns = list(
        Campaign.objects.filter(donations__in=base_donations)
        .select_related("client")
        .distinct()
        .order_by("name")[:100]
    )
    available_batches = list(
        DonationBatch.objects.filter(donations__in=base_donations)
        .select_related("campaign")
        .distinct()
        .order_by("-created_at")[:50]
    )

    return {
        "banking_date": banking_date,
        "date_from": date_from,
        "date_to": date_to,
        "page_obj": page_obj,
        "banking_batches": page_obj.object_list,
        "donations_page_obj": donations_page_obj,
        "dashboard_donations": donations_page_obj.object_list,
        "total_banking_batches": total_banking_batches,
        "client_count": client_count,
        "total_unassigned": totals["total_unassigned"] or 0,
        "total_amount": totals["total_amount"] or Decimal("0"),
        "bankable_payment_options": [
            {"value": payment_type, "label": PAYMENT_TYPE_LABELS[payment_type]}
            for payment_type in BANKABLE_PAYMENT_TYPES
        ],
        "available_clients": available_clients,
        "available_campaigns": available_campaigns,
        "available_batches": available_batches,
        "filter_client_id": filters.client_id,
        "filter_campaign_id": filters.campaign_id,
        "filter_payment_method": filters.payment_method,
        "filter_batch_id": filters.batch_id,
        "date_from_iso": date_from.isoformat(),
        "date_to_iso": date_to.isoformat(),
    }


def build_banking_batch_queue_context(request: HttpRequest) -> dict[str, object]:
    """Context for the HTMX batch queue partial on the daily banking dashboard."""
    batches_qs = build_banking_batches_queryset(request)
    page_obj = paginate_queryset(batches_qs, request, per_page=25)
    return {
        "page_obj": page_obj,
        "banking_batches": page_obj.object_list,
    }


def _querystring_with_page(request: HttpRequest, page_number: int) -> str:
    q = request.GET.copy()
    q["page"] = str(page_number)
    return q.urlencode()


def build_banking_batch_detail_context(
    request: HttpRequest, batch_id: int
) -> dict[str, object]:
    """Context for a single batch banking drill-down with transparency counts."""
    banking_date, date_from, date_to = get_date_range(request)
    batch = get_object_or_404(
        DonationBatch.objects.select_related("campaign", "campaign__client"),
        pk=batch_id,
    )
    banking_q = base_unassigned_filter(date_from, date_to)
    eligible_qs = (
        batch.donations.filter(banking_q)
        .select_related(
            "campaign",
            "campaign__client",
            "batch",
            "donor",
            "data_file_donor",
        )
        .order_by("payment_method", "-donation_date", "-created_at")
    )
    page_obj = paginate_queryset(eligible_qs, request, per_page=50)

    eligible_totals = batch.donations.filter(banking_q).aggregate(
        ec=Count("id"),
        esum=Sum("amount"),
    )
    eligible_total_count = int(eligible_totals["ec"] or 0)
    eligible_total_amount = cast(
        Decimal,
        eligible_totals["esum"]
        if eligible_totals["esum"] is not None
        else Decimal("0"),
    )

    preview_qs = (
        batch.donations.filter(banking_q)
        .select_related("donor", "data_file_donor")
        .order_by("payment_method", "-donation_date")[:SLIP_MODAL_PREVIEW_MAX]
    )
    slip_preview_items: list[dict[str, str]] = []
    for d in preview_qs:
        slip_preview_items.append(_donation_slip_preview_dict(d))

    eligible_for_slip = batch.donations.filter(banking_q)
    slip_payment_type_key = resolve_slip_payment_type(eligible_for_slip)
    slip_payment_type_label = (
        PAYMENT_TYPE_LABELS.get(slip_payment_type_key, "Mixed")
        if slip_payment_type_key != "mixed"
        else "Mixed"
    )

    approved_qs = batch.donations.filter(qa_status=Donation.QA_STATUS_APPROVED)
    approved_total = approved_qs.count()
    excluded_non_bankable = approved_qs.exclude(
        payment_method__in=BANKABLE_PAYMENT_TYPES
    ).count()
    excluded_on_slip = approved_qs.filter(
        payment_method__in=BANKABLE_PAYMENT_TYPES,
        paying_in_slip__isnull=False,
    ).count()
    unslipped_bankable = approved_qs.filter(
        payment_method__in=BANKABLE_PAYMENT_TYPES,
        paying_in_slip__isnull=True,
    )
    eligible_count = unslipped_bankable.filter(
        donation_banking_date_window_q(date_from, date_to)
    ).count()
    excluded_outside_date = unslipped_bankable.count() - eligible_count
    rejected_total = batch.donations.filter(
        qa_status=Donation.QA_STATUS_REJECTED
    ).count()

    prev_qs: str | None = None
    next_qs: str | None = None
    if page_obj.has_previous():
        prev_qs = _querystring_with_page(request, page_obj.number - 1)
    if page_obj.has_next():
        next_qs = _querystring_with_page(request, page_obj.number + 1)

    batch_total_donations = batch.donations.count()

    return {
        "batch": batch,
        "batch_total_donations": batch_total_donations,
        "slip_modal_preview_max": SLIP_MODAL_PREVIEW_MAX,
        "banking_date": banking_date,
        "date_from": date_from,
        "date_to": date_to,
        "date_from_iso": date_from.isoformat(),
        "date_to_iso": date_to.isoformat(),
        "page_obj": page_obj,
        "donations": page_obj.object_list,
        "transparency": {
            "approved_total": approved_total,
            "excluded_non_bankable": excluded_non_bankable,
            "excluded_on_slip": excluded_on_slip,
            "excluded_outside_date": max(0, excluded_outside_date),
            "rejected_total": rejected_total,
        },
        "pagination_prev_qs": prev_qs,
        "pagination_next_qs": next_qs,
        "eligible_total_count": eligible_total_count,
        "eligible_total_amount": eligible_total_amount,
        "slip_preview_items": slip_preview_items,
        "slip_preview_overflow": max(0, eligible_total_count - len(slip_preview_items)),
        "slip_payment_type_key": slip_payment_type_key,
        "slip_payment_type_label": slip_payment_type_label,
    }


def parse_slip_creation_payload(request: HttpRequest) -> SlipCreationPayload:
    """Parse and normalize paying-in-slip POST input."""
    return SlipCreationPayload(
        slip_number=request.POST.get("slip_number", "").strip(),
        banking_date_str=request.POST.get("banking_date"),
        notes=request.POST.get("notes", ""),
        batch_ids=_parse_json_list(request.POST.get("batch_ids", "[]")),
        donation_ids=_parse_json_list(request.POST.get("donation_ids", "[]")),
        date_from_str=(request.POST.get("date_from") or "").strip() or None,
        date_to_str=(request.POST.get("date_to") or "").strip() or None,
    )


def _parse_json_list(raw_value: str) -> list[Any]:
    """Parse a JSON array field from POST data."""
    parsed = json.loads(raw_value)
    if not isinstance(parsed, list):
        msg = "Expected a JSON array"
        raise ValueError(msg)
    return parsed


def validate_slip_creation_payload(payload: SlipCreationPayload) -> str | None:
    """Validate create-slip input and return a user-facing error if invalid."""
    if not payload.slip_number:
        return "Slip reference number is required."
    if PayingInSlip.objects.filter(slip_number__iexact=payload.slip_number).exists():
        return f"Slip reference '{payload.slip_number}' already exists."
    if not payload.batch_ids and not payload.donation_ids:
        return "No batches or donations selected."
    return None


def resolve_slip_banking_date_window(payload: SlipCreationPayload) -> tuple[date, date]:
    """Match dashboard date range when batch_ids are used; else single banking day."""
    if payload.date_from_str and payload.date_to_str:
        return (
            parse_date_param(payload.date_from_str),
            parse_date_param(payload.date_to_str),
        )
    if payload.banking_date_str:
        d = parse_date_param(payload.banking_date_str)
        return d, d
    return date.today(), date.today()


def fetch_slip_donations(payload: SlipCreationPayload) -> Any:
    """Return the queryset of donations eligible for a new paying-in slip."""
    if payload.batch_ids:
        date_from, date_to = resolve_slip_banking_date_window(payload)
        banking_q = base_unassigned_filter(date_from, date_to)
        return Donation.objects.filter(
            banking_q,
            batch_id__in=payload.batch_ids,
        ).select_related("campaign__client")

    donation_q = base_unassigned_core_filter() & Q(id__in=payload.donation_ids)
    if payload.date_from_str and payload.date_to_str:
        date_from, date_to = resolve_slip_banking_date_window(payload)
        donation_q &= donation_banking_date_window_q(date_from, date_to)
    return Donation.objects.filter(donation_q).select_related("campaign__client")


def resolve_slip_payment_type(donations_qs: Any) -> str:
    """Return the payment type label stored on the paying-in slip."""
    payment_methods = set(donations_qs.values_list("payment_method", flat=True))
    return payment_methods.pop() if len(payment_methods) == 1 else "mixed"


def resolve_slip_clients(donations_qs: Any) -> list[dict[str, Any]]:
    """Return distinct client rows represented by the donations queryset."""
    return list(
        donations_qs.values(
            cid=F("campaign__client__id"),
            cname=F("campaign__client__name"),
        )
        .distinct()
        .order_by("cname")
    )


def build_slip_success_payload(
    slip: PayingInSlip,
    updated_count: int,
    clients: list[dict[str, Any]],
) -> dict[str, object]:
    """Serialize the successful create-slip response body."""
    return {
        "success": True,
        "slip_id": slip.id,
        "slip_number": slip.slip_number,
        "donations_added": updated_count,
        "total_amount": str(slip.total_amount),
        "client_names": [client["cname"] for client in clients],
    }
