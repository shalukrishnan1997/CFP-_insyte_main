"""Chart data generators for ApexCharts visualisations.

Each _chart_* function returns a dict suitable for ApexCharts rendering.
The _prepare_chart_data function routes to the correct generator.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncDay, TruncMonth, TruncWeek
from django.utils import timezone

from banking.models import PayingInSlip
from donations.models import Donation

from .helpers import _get_donor_name, _get_uk_region_for_postcode

# ── Shared widget helpers ─────────────────────────────────────────────


def _calc_trend(current: float, previous: float) -> float:
    """Calculate percentage trend between two values."""
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return ((current - previous) / previous) * 100


def _field_or_unknown(entry: dict[str, Any], field: str) -> str:
    """Return the field value or ``'Unknown'`` if falsy."""
    return entry[field] or "Unknown"


def _safe_total(entry: dict[str, Any]) -> float:
    """Return the ``total`` value as float, defaulting to ``0.0``."""
    return float(entry["total"] or 0)


def _build_periodic_lookup(
    qs: Any,
    field: str,
) -> tuple[list[str], list[str], dict[tuple[str, str], float]]:
    """Extract periods, field values, and a fast lookup from a periodic queryset."""
    valid = [e for e in qs if e["p"]]
    periods: list[str] = sorted({e["p"].strftime("%d %b") for e in valid})
    field_vals: list[str] = sorted({_field_or_unknown(e, field) for e in valid})
    lookup: dict[tuple[str, str], float] = {}
    for entry in valid:
        lookup[(entry["p"].strftime("%d %b"), _field_or_unknown(entry, field))] = (
            _safe_total(entry)
        )
    return periods, field_vals, lookup


def _get_periodic_data(
    donations: Any,
    trunc_func: Any,
    field: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Build periodic time-series data grouped by *field*."""
    qs = (
        donations.annotate(p=trunc_func("donation_date"))
        .values("p", field)
        .annotate(total=Sum("amount"))
        .order_by("p")
    )
    periods, field_vals, lookup = _build_periodic_lookup(qs, field)

    series: list[dict[str, Any]] = []
    for val in field_vals:
        data = [lookup.get((p_str, val), 0.0) for p_str in periods]
        series.append({"name": str(val).replace("_", " ").title(), "data": data})
    return periods, series


def _calc_distribution(
    all_donations: list,
    field: str,
    limit: int = 0,
) -> list[tuple[str, float]]:
    """Calculate amount distribution by *field* from in-memory donations."""
    stats: dict[str, float] = defaultdict(float)
    for d in all_donations:
        if field == "campaign":
            key = d.campaign.name if d.campaign else "Unknown"
        else:
            key = getattr(d, field, "Unknown") or "Unknown"
        stats[key] += float(d.amount or 0)
    result = sorted(stats.items(), key=lambda x: -x[1])
    return result[:limit] if limit else result


def _calc_cumulative_revenue(
    donations: Any,
) -> tuple[list[str], list[float]]:
    """Calculate cumulative daily revenue series."""
    daily_qs = (
        donations.annotate(day=TruncDay("donation_date"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    cumulative = 0.0
    x_vals: list[str] = []
    y_vals: list[float] = []
    for e in daily_qs:
        if e["day"]:
            cumulative += float(e["total"] or 0)
            x_vals.append(e["day"].strftime("%d %b"))
            y_vals.append(cumulative)
    return x_vals, y_vals


def _calc_hgv_vs_standard(
    all_donations: list,
) -> tuple[float, float]:
    """Split donations into high-value vs standard totals."""
    hgv = 0.0
    std = 0.0
    for d in all_donations:
        amt = float(d.amount or 0)
        limit = (
            float(d.campaign.hgv_amount)
            if d.campaign and d.campaign.hgv_amount
            else 1000.0
        )
        if amt >= limit:
            hgv += amt
        else:
            std += amt
    return hgv, std


_AMOUNT_BANDS = ("£0-£10", "£10-£50", "£50-£100", "> £100")
_BAND_THRESHOLDS = (10, 50, 100)


def _calc_amount_bands(all_donations: list) -> dict[str, int]:
    """Count donations in each amount band."""
    bands = {b: 0 for b in _AMOUNT_BANDS}
    for d in all_donations:
        amt = float(d.amount or 0)
        if amt <= _BAND_THRESHOLDS[0]:
            bands[_AMOUNT_BANDS[0]] += 1
        elif amt <= _BAND_THRESHOLDS[1]:
            bands[_AMOUNT_BANDS[1]] += 1
        elif amt <= _BAND_THRESHOLDS[2]:
            bands[_AMOUNT_BANDS[2]] += 1
        else:
            bands[_AMOUNT_BANDS[3]] += 1
    return bands


def _calc_geo_map(all_donations: list) -> dict[str, float]:
    """Calculate donation totals by UK region from postcode."""
    geo: dict[str, float] = defaultdict(float)
    for d in all_donations:
        if d.donor and d.donor.postcode:
            region = _get_uk_region_for_postcode(d.donor.postcode)
            if region != "Unknown":
                geo[region] += float(d.amount or 0)
    return dict(geo)


def _calc_qa_status(all_donations: list) -> tuple[int, int]:
    """Count QA approved vs other donations."""
    approved = sum(1 for d in all_donations if d.qa_status == "approved")
    return approved, max(0, len(all_donations) - approved)


def _get_periodic_counts(
    donations: Any,
    trunc_func: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Build periodic donation count series."""
    qs = (
        donations.annotate(p=trunc_func("donation_date"))
        .values("p")
        .annotate(cnt=Count("id"))
        .order_by("p")
    )
    periods = sorted({e["p"].strftime("%d %b") for e in qs if e["p"]})
    counts = []
    for p_str in periods:
        match = next(
            (e["cnt"] for e in qs if e["p"] and e["p"].strftime("%d %b") == p_str),
            0,
        )
        counts.append(match)
    return periods, [{"name": "Donation Count", "data": counts}]


def _calc_gift_aid_split(all_donations: list) -> tuple[float, float]:
    """Return (gift_aid_eligible_amount, non_eligible_amount)."""
    total = sum(float(d.amount or 0) for d in all_donations)
    eligible = sum(float(d.amount or 0) for d in all_donations if d.gift_aid)
    return eligible, max(0.0, total - eligible)


def _calc_top_donors(all_donations: list, limit: int = 10) -> list[tuple[str, float]]:
    """Return top *limit* donors by total donated amount."""
    totals: dict[str, float] = defaultdict(float)
    for d in all_donations:
        totals[_get_donor_name(d)] += float(d.amount or 0)
    return sorted(totals.items(), key=lambda x: -x[1])[:limit]


def _build_trend_widgets(
    donations: Any,
    all_donations: list,
    trunc_func: Any,
) -> dict[str, dict[str, Any]]:
    """Build optional trend count + top donors widgets."""
    count_cats, count_ser = _get_periodic_counts(donations, trunc_func)
    sorted_donors = _calc_top_donors(all_donations)
    return {
        "trend_count": {
            "type": "area_single",
            "title": "Donation Count Trend",
            "categories": count_cats,
            "series": count_ser,
        },
        "top_donors": {
            "type": "bar_horizontal",
            "title": "Top 10 Donors by Amount",
            "series": [
                {"name": "Total Donated", "data": [v for _, v in sorted_donors]},
            ],
            "categories": [k for k, _ in sorted_donors],
        },
    }


def _build_donation_widgets(
    donations: Any,
    all_donations: list,
    trunc_func: Any,
    include_count_trend: bool = True,
) -> dict[str, dict[str, Any]]:
    """Generate the standard set of donation dashboard widgets."""
    pm_sorted = _calc_distribution(all_donations, "payment_method")
    camp_sorted = _calc_distribution(all_donations, "campaign", limit=10)
    ga_eligible, ga_non = _calc_gift_aid_split(all_donations)

    p_cats, p_series = _get_periodic_data(donations, trunc_func, "payment_method")
    c_cats, c_series = _get_periodic_data(donations, trunc_func, "campaign__name")
    cum_x, cum_y = _calc_cumulative_revenue(donations)
    hgv_total, std_total = _calc_hgv_vs_standard(all_donations)
    bands = _calc_amount_bands(all_donations)
    geo = _calc_geo_map(all_donations)
    qa_ok, qa_other = _calc_qa_status(all_donations)

    widgets: dict[str, dict[str, Any]] = {
        "dist_payment": {
            "type": "donut",
            "title": "Payment Method Distribution",
            "series": [v for _, v in pm_sorted],
            "labels": [k.replace("_", " ").title() for k, _ in pm_sorted],
        },
        "dist_campaign": {
            "type": "donut",
            "title": "Campaign Code Distribution",
            "series": [v for _, v in camp_sorted],
            "labels": [k for k, _ in camp_sorted],
        },
        "dist_gift_aid": {
            "type": "pie",
            "title": "Gift Aid Status",
            "series": [ga_eligible, ga_non],
            "labels": ["Eligible", "Non-Eligible"],
        },
        "trend_payment": {
            "type": "bar_grouped",
            "title": "Payment Methods Over Time",
            "categories": p_cats,
            "series": p_series,
        },
        "trend_campaign": {
            "type": "bar_grouped",
            "title": "Campaign Codes Over Time",
            "categories": c_cats,
            "series": c_series,
        },
        "trend_revenue": {
            "type": "area_single",
            "title": "Cumulative Revenue Trend",
            "categories": cum_x,
            "series": [{"name": "Cumulative Revenue", "data": cum_y}],
        },
        "dist_high_value": {
            "type": "bar_column",
            "title": "High Value vs Standard Donations",
            "series": [{"name": "Total Amount", "data": [hgv_total, std_total]}],
            "categories": ["High Value", "Standard"],
        },
        "geo_map": {
            "type": "uk_map",
            "title": "Donation Density by Region",
            "data": geo,
        },
        "dist_sla": {
            "type": "donut",
            "title": "SLA Compliance Distribution",
            "series": [qa_ok, qa_other],
            "labels": ["Compliant", "Pending"],
        },
        "dist_bands": {
            "type": "donut",
            "title": "Donation Amount Bands",
            "series": list(bands.values()),
            "labels": list(bands.keys()),
        },
    }

    if include_count_trend:
        widgets.update(_build_trend_widgets(donations, all_donations, trunc_func))

    return widgets


def _chart_payment_method(donations: Any) -> dict[str, Any]:
    """Generate donut chart data for payment method distribution."""
    pm_data = (
        donations.values("payment_method")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    method_map = {
        "card": "Credit/Debit Card",
        "direct_debit": "Direct Debit",
        "cash": "Cash",
        "caf": "CAF Voucher",
        "cheque": "Cheque",
        "postal_order": "Postal Order",
        "postal_cheque": "Postal Order",
        "non_financial": "Non Financial",
    }
    labels: list[str] = []
    series: list[float] = []
    counts: list[int] = []
    for pm in pm_data:
        raw = pm["payment_method"] or "Unknown"
        labels.append(method_map.get(raw, raw.replace("_", " ").title()))
        series.append(float(pm["total"] or 0))
        counts.append(pm["count"])
    return {
        "type": "donut",
        "series": series,
        "labels": labels,
        "counts": counts,
        "title": "Donations by Payment Method",
    }


def _chart_roi(donations: Any) -> dict[str, Any]:
    """Generate horizontal bar chart for campaign performance vs target."""
    campaign_data = (
        donations.values("campaign__name", "campaign__target_amount")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")[:15]
    )
    categories: list[str] = []
    raised: list[float] = []
    targets: list[float] = []
    for cd in campaign_data:
        categories.append(cd["campaign__name"] or "N/A")
        raised.append(float(cd["total"] or 0))
        targets.append(float(cd["campaign__target_amount"] or 0))
    return {
        "type": "bar_horizontal",
        "series": [
            {"name": "Raised", "data": raised},
            {"name": "Target", "data": targets},
        ],
        "categories": categories,
        "title": "Campaign Performance vs Target",
    }


def _chart_campaign_summary(donations: Any) -> dict[str, Any]:
    """Generate pie chart for donation share by campaign."""
    campaign_data = (
        donations.values("campaign__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")[:12]
    )
    labels: list[str] = []
    series: list[float] = []
    for cd in campaign_data:
        labels.append(cd["campaign__name"] or "N/A")
        series.append(float(cd["total"] or 0))
    return {
        "type": "pie",
        "series": series,
        "labels": labels,
        "title": "Donation Share by Campaign",
    }


def _chart_gift_aid(donations: Any) -> dict[str, Any]:
    """Generate stacked bar chart — gift aid eligible vs non-eligible by month."""

    monthly = (
        donations.annotate(
            month=TruncMonth("donation_date"),
        )
        .values("month")
        .annotate(
            eligible=Sum("amount", filter=Q(gift_aid=True)),
            non_eligible=Sum("amount", filter=Q(gift_aid=False)),
        )
        .order_by("month")
    )
    categories: list[str] = []
    eligible_data: list[float] = []
    non_eligible_data: list[float] = []
    for m in monthly:
        if m["month"]:
            categories.append(m["month"].strftime("%b %Y"))
            eligible_data.append(float(m["eligible"] or 0))
            non_eligible_data.append(float(m["non_eligible"] or 0))
    return {
        "type": "bar_stacked",
        "series": [
            {"name": "Gift Aid Eligible", "data": eligible_data},
            {"name": "Non-Eligible", "data": non_eligible_data},
        ],
        "categories": categories,
        "title": "Gift Aid Eligible vs Non-Eligible by Month",
    }


def _chart_donor_summary(donations: Any) -> dict[str, Any]:
    """Generate bar chart for top donors by total amount."""

    donor_totals: dict[str, float] = defaultdict(float)
    for d in donations:
        name = _get_donor_name(d)
        donor_totals[name] += float(d.amount or 0)
    sorted_donors = sorted(donor_totals.items(), key=lambda x: -x[1])[:15]
    categories = [d[0] for d in sorted_donors]
    values = [d[1] for d in sorted_donors]
    return {
        "type": "bar",
        "series": [{"name": "Total Donated", "data": values}],
        "categories": categories,
        "title": "Top Donors by Total Amount",
    }


def _resolve_date_range(
    date_from: Any,
    date_to: Any,
) -> tuple[Any, Any, int]:
    """Normalise date_from/date_to to date objects and compute duration."""
    now = timezone.now().date()
    if not date_from:
        date_from = now - timedelta(days=30)
    if not date_to:
        date_to = now
    if hasattr(date_from, "date"):
        date_from = date_from.date()
    if hasattr(date_to, "date"):
        date_to = date_to.date()
    duration = (date_to - date_from).days + 1
    return date_from, date_to, duration


def _pick_trunc_func(duration: int) -> Any:
    """Choose the appropriate truncation function based on duration."""
    if duration <= 60:
        return TruncDay
    if duration <= 90:
        return TruncWeek
    return TruncMonth


def _build_prev_qs(
    date_from: Any,
    duration: int,
    scope: str,
    client_id: str,
    campaign_id: str,
) -> Any:
    """Build the previous-period queryset for trend comparison."""
    prev_date_to = date_from - timedelta(days=1)
    prev_date_from = prev_date_to - timedelta(days=duration - 1)

    prev_qs = Donation.objects.filter(
        Q(donation_date__gte=prev_date_from, donation_date__lte=prev_date_to)
        | Q(
            donation_date__isnull=True,
            created_at__date__gte=prev_date_from,
            created_at__date__lte=prev_date_to,
        )
    )
    if scope == "client" and client_id:
        prev_qs = prev_qs.filter(campaign__client_id=client_id)
    elif scope == "campaign" and campaign_id:
        prev_qs = prev_qs.filter(campaign_id=campaign_id)
    return prev_qs


def _summarize_donations(
    donation_list: list,
) -> tuple[float, int, float, float]:
    """Return (total_amount, count, avg_amount, gift_aid_value) for a list."""
    total = sum(float(d.amount or 0) for d in donation_list)
    count = len(donation_list)
    avg = total / count if count else 0.0
    ga_total = sum(float(d.amount or 0) for d in donation_list if d.gift_aid)
    return total, count, avg, ga_total * 0.25


def _build_kpi(
    all_donations: list,
    prev_donations: list,
    duration: int,
) -> dict[str, Any]:
    """Calculate KPI summary with trends."""
    total, count, avg, ga_val = _summarize_donations(all_donations)
    prev_total, prev_count, prev_avg, prev_ga_val = _summarize_donations(prev_donations)
    ga_total = sum(float(d.amount or 0) for d in all_donations if d.gift_aid)

    return {
        "total_amount": total,
        "total_count": count,
        "avg_amount": avg,
        "gift_aid_value": ga_val,
        "gift_aid_eligible_total": float(ga_total),
        "trends": {
            "total_amount": _calc_trend(total, prev_total),
            "total_count": _calc_trend(float(count), float(prev_count)),
            "avg_amount": _calc_trend(avg, prev_avg),
            "gift_aid_value": _calc_trend(ga_val, prev_ga_val),
        },
        "period_label": f"vs previous {duration} days",
    }


def _chart_donations(
    donations: Any,
    date_from: Any = None,
    date_to: Any = None,
    scope: str = "overall",
    client_id: str = "",
    campaign_id: str = "",
) -> dict[str, Any]:
    """Generate multi-widget dashboard data for All Donations report."""
    all_donations = list(
        donations.select_related("campaign", "donor", "data_file_donor")
    )

    date_from, date_to, duration = _resolve_date_range(date_from, date_to)
    trunc_func = _pick_trunc_func(duration)

    prev_qs = _build_prev_qs(date_from, duration, scope, client_id, campaign_id)
    prev_donations = list(prev_qs)

    kpi = _build_kpi(all_donations, prev_donations, duration)
    widgets = _build_donation_widgets(
        donations,
        all_donations,
        trunc_func,
        include_count_trend=True,
    )

    return {
        "type": "dashboard",
        "kpi": kpi,
        "widgets": widgets,
        "scope": scope,
    }


def _chart_banking(donations: Any) -> dict[str, Any]:
    """Generate bar chart for banking totals by month."""

    bank_only = donations.filter(payment_method="direct_debit")
    monthly = (
        bank_only.annotate(month=TruncMonth("donation_date"))
        .values("month")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("month")
    )
    categories: list[str] = []
    totals: list[float] = []
    for m in monthly:
        if m["month"]:
            categories.append(m["month"].strftime("%b %Y"))
            totals.append(float(m["total"] or 0))
    return {
        "type": "bar",
        "series": [{"name": "Banking Total", "data": totals}],
        "categories": categories,
        "title": "Banking Donations by Month",
    }


def _chart_credit_card(donations: Any) -> dict[str, Any]:
    """Generate line chart for credit card donations over time."""

    card_only = donations.filter(payment_method="card")
    monthly = (
        card_only.annotate(month=TruncMonth("donation_date"))
        .values("month")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("month")
    )
    categories: list[str] = []
    totals: list[float] = []
    for m in monthly:
        if m["month"]:
            categories.append(m["month"].strftime("%b %Y"))
            totals.append(float(m["total"] or 0))
    return {
        "type": "line",
        "series": [{"name": "Credit Card Donations", "data": totals}],
        "categories": categories,
        "title": "Credit Card Donations Over Time",
    }


def _chart_hgv(donations: Any) -> dict[str, Any]:
    """Generate bar chart for HGV donations by campaign."""
    DEFAULT_HGV = Decimal("1000.00")
    campaign_totals: dict[str, float] = {}
    for d in donations:
        if not d.campaign:
            continue
        amount = d.amount or Decimal("0")
        threshold = d.campaign.hgv_amount if d.campaign.hgv_amount > 0 else DEFAULT_HGV
        if amount >= threshold:
            name = d.campaign.name
            campaign_totals[name] = campaign_totals.get(name, 0) + float(amount)
    sorted_items = sorted(campaign_totals.items(), key=lambda x: -x[1])[:15]
    return {
        "type": "bar",
        "series": [{"name": "HGV Donations", "data": [v for _, v in sorted_items]}],
        "categories": [k for k, _ in sorted_items],
        "title": "High Gift Value Donations by Campaign",
    }


def _chart_lgv(donations: Any) -> dict[str, Any]:
    """Generate bar chart for LGV donations by campaign."""
    DEFAULT_LGV = Decimal("50.00")
    campaign_totals: dict[str, float] = {}
    for d in donations:
        if not d.campaign:
            continue
        amount = d.amount or Decimal("0")
        threshold = d.campaign.lgv_amount if d.campaign.lgv_amount > 0 else DEFAULT_LGV
        if amount <= threshold:
            name = d.campaign.name
            campaign_totals[name] = campaign_totals.get(name, 0) + float(amount)
    sorted_items = sorted(campaign_totals.items(), key=lambda x: -x[1])[:15]
    return {
        "type": "bar",
        "series": [{"name": "LGV Donations", "data": [v for _, v in sorted_items]}],
        "categories": [k for k, _ in sorted_items],
        "title": "Low Gift Value Donations by Campaign",
    }


def _chart_paying_in_slips(
    date_from: Any = None,
    date_to: Any = None,
    client_id: str = "",
) -> dict[str, Any]:
    """Generate bar chart for paying-in slip totals by date."""

    slips = PayingInSlip.objects.all()
    if date_from:
        slips = slips.filter(banking_date__gte=date_from)
    if date_to:
        slips = slips.filter(banking_date__lte=date_to)
    if client_id:
        slips = slips.filter(client_id=client_id)

    daily = (
        slips.annotate(day=TruncDate("banking_date"))
        .values("day")
        .annotate(total=Sum("total_amount"), count=Count("id"))
        .order_by("day")
    )
    categories: list[str] = []
    totals: list[float] = []
    for d in daily:
        if d["day"]:
            categories.append(d["day"].strftime("%d %b"))
            totals.append(float(d["total"] or 0))
    return {
        "type": "bar",
        "series": [{"name": "Slip Total", "data": totals}],
        "categories": categories,
        "title": "Paying-In Slips by Date",
    }


_CHART_GENERATORS: dict[str, Any] = {
    "donations": _chart_donations,
    "payment_method": _chart_payment_method,
    "credit_card": _chart_credit_card,
    "banking": _chart_banking,
    "gift_aid": _chart_gift_aid,
    "roi": _chart_roi,
    "campaign_summary": _chart_campaign_summary,
    "hgv": _chart_hgv,
    "lgv": _chart_lgv,
}


def _prepare_chart_data(
    report_type: str,
    donations: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Route to the correct chart generator and return chart data dict.

    Note: ``paying_in_slips`` is handled as a special case because it
    queries ``PayingInSlip`` directly rather than the donations queryset.
    All other report types follow the standard ``_CHART_GENERATORS`` dispatch.
    """
    if report_type == "paying_in_slips":
        # Special case: this report queries PayingInSlip, not Donation.
        # It needs date/client kwargs that are not available via the donations QS.
        try:
            return _chart_paying_in_slips(
                date_from=kwargs.get("date_from"),
                date_to=kwargs.get("date_to"),
                client_id=kwargs.get("client_id", ""),
            )
        except Exception:
            return None

    generator = _CHART_GENERATORS.get(report_type)
    if not generator:
        return None

    try:
        if report_type == "donations":
            return generator(donations, **kwargs)
        return generator(donations)
    except Exception:
        return None


def _get_campaign_widgets(
    donations: object,
    campaign_id: str,
    trunc_func: object,
    all_donations: list,
) -> dict:
    """Generate widgets specifically for the Elite Campaign Dashboard view."""
    return _build_donation_widgets(
        donations,
        all_donations,
        trunc_func,
        include_count_trend=False,
    )
