"""Daily banking constants and shared helpers.

Used by the dashboard, HTMX partials, and slip management views.
"""

from datetime import date
from typing import Any

from django.db.models import Q
from django.http import HttpRequest

from core.date_utils import parse_date_with_default

# Payment types eligible for paying-in slips (physical payments only).
BANKABLE_PAYMENT_TYPES = [
    "cash",
    "cheque",
    "postal_order",
    "caf",
]

PAYMENT_TYPE_LABELS: dict[str, str] = {
    "cash": "Cash",
    "cheque": "Cheque",
    "postal_order": "Postal Order",
    "caf": "CAF Voucher",
}

PAYMENT_TYPE_COLORS: dict[str, str] = {
    "cash": "emerald",
    "cheque": "blue",
    "postal_order": "violet",
    "caf": "amber",
}


# ---------------------------------------------------------------------------
# Date / filter helpers
# ---------------------------------------------------------------------------


def parse_date_param(date_str: str | None, default: date | None = None) -> date:
    """Parse a date string from a request parameter.

    Supports ``YYYY-MM-DD`` (ISO) and ``DD/MM/YYYY`` (UK) formats.

    Args:
        date_str: Raw date string.
        default: Fallback date (defaults to today).

    Returns:
        Parsed date or *default*.
    """
    return parse_date_with_default(date_str, default=default)


def donation_banking_date_window_q(date_from: date, date_to: date) -> Q:
    """Donation date (or created date) within the banking dashboard range."""
    return Q(donation_date__gte=date_from, donation_date__lte=date_to) | Q(
        donation_date__isnull=True,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )


def base_unassigned_core_filter() -> Q:
    """Eligibility rules that do not depend on a date window."""
    return (
        Q(paying_in_slip__isnull=True)
        & Q(payment_method__in=BANKABLE_PAYMENT_TYPES)
        & Q(qa_status="approved")
    )


def base_unassigned_filter(date_from: date, date_to: date) -> Q:
    """Build a Q filter for unassigned, bankable, approved donations.

    Args:
        date_from: Range start.
        date_to: Range end.

    Returns:
        Combined ``Q`` object.
    """
    return base_unassigned_core_filter() & donation_banking_date_window_q(
        date_from, date_to
    )


def get_date_range(request: HttpRequest) -> tuple[date, date, date]:
    """Extract ``banking_date``, ``date_from``, ``date_to`` from GET params.

    Args:
        request: HTTP request.

    Returns:
        ``(banking_date, date_from, date_to)`` tuple.
    """
    banking_date = parse_date_param(request.GET.get("date"))
    date_from = (
        parse_date_param(request.GET.get("date_from"))
        if request.GET.get("date_from")
        else banking_date
    )
    date_to = (
        parse_date_param(request.GET.get("date_to"))
        if request.GET.get("date_to")
        else banking_date
    )
    return banking_date, date_from, date_to


def apply_donation_filters(
    qs: Any,
    client_id: str | None = None,
    campaign_id: str | None = None,
    payment_method: str | None = None,
    batch_id: str | None = None,
) -> Any:
    """Apply optional filters to a donations queryset."""
    model_name = getattr(getattr(qs, "model", None), "_meta", None)
    is_campaign_queryset = getattr(model_name, "model_name", "") == "campaign"

    client_lookup = "client_id" if is_campaign_queryset else "campaign__client_id"
    campaign_lookup = "id" if is_campaign_queryset else "campaign_id"
    payment_lookup = (
        "donations__payment_method" if is_campaign_queryset else "payment_method"
    )
    batch_lookup = "donations__batch_id" if is_campaign_queryset else "batch_id"

    if client_id:
        qs = qs.filter(**{client_lookup: client_id})
    if campaign_id:
        qs = qs.filter(**{campaign_lookup: campaign_id})
    if payment_method:
        qs = qs.filter(**{payment_lookup: payment_method})
    if batch_id:
        qs = qs.filter(**{batch_lookup: batch_id})
    return qs
