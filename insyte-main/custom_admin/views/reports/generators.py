"""Report data generators.

Each function takes a donations queryset and returns a list of
row data (list[list[str]]) for rendering in tables or exporting.
"""

from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Avg, Case, Count, DecimalField, F, Q, Sum, Value, When

from banking.models import PayingInSlip

from .helpers import _get_donor_address, _get_donor_name, _get_donor_urn

# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------


def _fmt_date(d: Any) -> str:
    """Return formatted donation date string."""
    dt = d.donation_date if d.donation_date else d.created_at.date()
    return dt.strftime("%d/%m/%Y") if dt else "-"


def _fmt_amount(amount: Any) -> str:
    """Return '£X.XX' or '£0.00'."""
    return f"£{amount:.2f}" if amount else "£0.00"


def _campaign_name(d: Any) -> str:
    """Return campaign name or 'N/A'."""
    return d.campaign.name if d.campaign else "N/A"


def _attr_or_dash(obj: Any, attr: str) -> str:
    """Return getattr value or '-'."""
    return getattr(obj, attr, "") or "-"


def _payment_display(d: Any) -> str:
    """Return human-readable payment method."""
    if hasattr(d, "get_payment_method_display"):
        return d.get_payment_method_display()
    return d.payment_method or "-"


def _generate_donations_report(donations: Any) -> list[list[str]]:
    """Generate all donations report data."""
    return [
        [
            _fmt_date(d),
            _get_donor_name(d),
            _get_donor_urn(d),
            _campaign_name(d),
            _fmt_amount(d.amount),
            _payment_display(d),
            "Yes" if getattr(d, "gift_aid", False) else "No",
        ]
        for d in donations
    ]


def _generate_payment_method_report(donations: Any) -> list[list[str]]:
    """Generate payment method breakdown report."""
    totals = donations.aggregate(grand_total=Sum("amount"))
    grand_total = totals["grand_total"] or Decimal("0")

    pm_data = (
        donations.values("payment_method")
        .annotate(
            total=Sum("amount"),
            count=Count("id"),
            avg=Avg("amount"),
        )
        .order_by("-total")
    )

    data = []
    for pm in pm_data:
        method_display = pm["payment_method"] or "Unknown"
        # Convert to readable display name
        method_map = {
            "card": "Credit/Debit Card",
            "direct_debit": "Direct Debit",
            "cash": "Cash",
            "caf": "CAF Voucher",
            "cheque": "Cheque",
            "postal_order": "Postal Order",
            "non_financial": "Non Financial/No Payment",
        }
        display = method_map.get(
            method_display, method_display.replace("_", " ").title()
        )
        total_val = pm["total"] or Decimal("0")
        pct = (total_val / grand_total * 100) if grand_total else 0
        data.append(
            [
                display,
                str(pm["count"]),
                f"£{total_val:.2f}",
                f"£{pm['avg']:.2f}" if pm["avg"] else "£0.00",
                f"{pct:.1f}%",
            ]
        )
    return data


def _generate_credit_card_report(donations: Any) -> list[list[str]]:
    """Generate credit card donations report."""
    card_donations = donations.filter(payment_method="card")
    include_card_cols = getattr(settings, "ALLOW_CARD_METADATA_EXPORT", True)
    rows: list[list[str]] = []
    for d in card_donations:
        row = [
            _fmt_date(d),
            _get_donor_name(d),
            _get_donor_urn(d),
            _campaign_name(d),
            _fmt_amount(d.amount),
        ]
        if include_card_cols:
            row.extend(
                [
                    _attr_or_dash(d, "card_holder_name"),
                    _attr_or_dash(d, "card_last_four"),
                    _attr_or_dash(d, "card_expiry_date"),
                ]
            )
        rows.append(row)
    return rows


def _generate_banking_report(donations: Any) -> list[list[str]]:
    """Generate banking report for direct debits."""
    bank_donations = donations.filter(payment_method="direct_debit").select_related(
        "paying_in_slip"
    )
    return [
        [
            _fmt_date(d),
            _get_donor_name(d),
            _get_donor_urn(d),
            _campaign_name(d),
            _fmt_amount(d.amount),
            d.paying_in_slip.slip_number if d.paying_in_slip else "-",
        ]
        for d in bank_donations
    ]


def _generate_gift_aid_report(donations: Any) -> list[list[str]]:
    """Generate Gift Aid eligible donations report."""
    gift_aid_donations = donations.filter(gift_aid=True)
    data = []
    for d in gift_aid_donations:
        address, postcode = _get_donor_address(d)
        amount = d.amount or Decimal("0")
        gift_aid_amount = amount * Decimal("0.25")  # UK Gift Aid rate
        display_date = d.donation_date if d.donation_date else d.created_at.date()
        data.append(
            [
                display_date.strftime("%d/%m/%Y") if display_date else "-",
                _get_donor_name(d),
                _get_donor_urn(d),
                address or "-",
                postcode or "-",
                d.campaign.name if d.campaign else "N/A",
                f"£{amount:.2f}",
                f"£{gift_aid_amount:.2f}",
            ]
        )
    return data


def _format_roi_row(cd: dict[str, Any]) -> list[str]:
    """Format a single campaign ROI row."""
    total_val = cd["total"] or Decimal("0")
    target = cd["campaign__target_amount"] or Decimal("0")
    performance = (total_val / target * 100) if target else 0
    return [
        cd["campaign__name"] or "N/A",
        f"£{total_val:.2f}",
        str(cd["count"]),
        f"£{cd['avg']:.2f}" if cd["avg"] else "£0.00",
        f"£{target:.2f}" if target else "Not Set",
        f"{performance:.1f}%" if target else "N/A",
    ]


def _generate_roi_report(donations: Any) -> list[list[str]]:
    """Generate ROI report by campaign.

    Note: Campaign cost field is not currently implemented.
    ROI calculation shows target vs actual performance.
    """
    campaign_data = (
        donations.values("campaign__name", "campaign__target_amount")
        .annotate(
            total=Sum("amount"),
            count=Count("id"),
            avg=Avg("amount"),
        )
        .order_by("-total")
    )
    return [_format_roi_row(cd) for cd in campaign_data]


_DEFAULT_HGV_THRESHOLD = Decimal("1000.00")
_DEFAULT_LGV_THRESHOLD = Decimal("50.00")
_DECIMAL_FIELD = DecimalField(max_digits=10, decimal_places=2)


def _threshold_case(field: str, default: Decimal) -> Case:
    """Coalesce a campaign threshold (>0) to a default when unset."""
    return Case(
        When(**{f"campaign__{field}__gt": 0}, then=F(f"campaign__{field}")),
        default=Value(default),
        output_field=_DECIMAL_FIELD,
    )


def _format_gift_value_row(d: Any, threshold: Decimal, delta: Decimal) -> list[str]:
    """Shared row formatter for HGV/LGV reports."""
    amount = d.amount or Decimal("0")
    display_date = d.donation_date or d.created_at.date()
    return [
        display_date.strftime("%d/%m/%Y") if display_date else "-",
        _get_donor_name(d),
        _get_donor_urn(d),
        d.campaign.name,
        f"£{amount:.2f}",
        f"£{threshold:.2f}",
        f"£{delta:.2f}",
    ]


def _generate_hgv_report(donations: Any) -> list[list[str]]:
    """Generate High Gift Value report.

    Shows donations at or above the campaign's HGV threshold.
    Campaigns without HGV thresholds use a default of £1000.
    """
    qs = (
        donations.annotate(
            effective_threshold=_threshold_case("hgv_amount", _DEFAULT_HGV_THRESHOLD)
        )
        .filter(amount__gte=F("effective_threshold"))
        .annotate(above_by=F("amount") - F("effective_threshold"))
    )
    return [_format_gift_value_row(d, d.effective_threshold, d.above_by) for d in qs]


def _generate_lgv_report(donations: Any) -> list[list[str]]:
    """Generate Low Gift Value report.

    Shows donations at or below the campaign's LGV threshold.
    Campaigns without LGV thresholds use a default of £50.
    """
    qs = (
        donations.annotate(
            effective_threshold=_threshold_case("lgv_amount", _DEFAULT_LGV_THRESHOLD)
        )
        .filter(amount__lte=F("effective_threshold"))
        .annotate(below_by=F("effective_threshold") - F("amount"))
    )
    return [_format_gift_value_row(d, d.effective_threshold, d.below_by) for d in qs]


def _generate_campaign_summary_report(donations: Any) -> list[list[str]]:
    """Generate campaign summary report."""
    campaign_data = (
        donations.values("campaign__name", "campaign__client__name")
        .annotate(
            total=Sum("amount"),
            count=Count("id"),
            avg=Avg("amount"),
            ga_total=Sum("amount", filter=Q(gift_aid=True)),
        )
        .order_by("-total")
    )

    data = []
    for cd in campaign_data:
        ga_total = cd["ga_total"] or Decimal("0")
        data.append(
            [
                cd["campaign__name"] or "N/A",
                cd["campaign__client__name"] or "N/A",
                f"£{cd['total']:.2f}" if cd["total"] else "£0.00",
                str(cd["count"]),
                f"£{cd['avg']:.2f}" if cd["avg"] else "£0.00",
                f"£{ga_total * Decimal('0.25'):.2f}",
            ]
        )
    return data


def _slip_created_by(slip: Any) -> str:
    """Return creator display name or '-'."""
    user = slip.created_by
    if not user:
        return "-"
    return user.get_full_name() or user.username or "-"


def _format_slip_row(slip: Any) -> list[str]:
    """Format a single paying-in slip row."""
    return [
        slip.slip_number or "-",
        slip.client.name if slip.client else "-",
        slip.banking_date.strftime("%d/%m/%Y") if slip.banking_date else "-",
        slip.get_payment_type_display() if slip.payment_type else "-",
        str(slip.total_items),
        f"£{slip.total_amount:.2f}",
        slip.get_status_display() if slip.status else "-",
        _slip_created_by(slip),
    ]


def _generate_paying_in_slips_report(
    *,
    date_from: Any = None,
    date_to: Any = None,
    client_id: str = "",
) -> list[list[str]]:
    """Generate paying-in slips report.

    This report queries the PayingInSlip model directly rather than
    using the donations queryset, since slips are independent records.

    Args:
        date_from: Optional start date filter.
        date_to: Optional end date filter.
        client_id: Optional client ID filter.

    Returns:
        List of row data for the report table.
    """
    slips = PayingInSlip.objects.select_related("client", "created_by").order_by(
        "-banking_date"
    )

    if date_from:
        slips = slips.filter(banking_date__gte=date_from)
    if date_to:
        slips = slips.filter(banking_date__lte=date_to)
    if client_id:
        slips = slips.filter(client_id=client_id)

    return [_format_slip_row(s) for s in slips]


def _format_unmatched_row(donor: Any, campaign_names: set[str]) -> list[str]:
    """Format one ``unmatched_donors`` row from a Donor or SystemDonor.

    Both ``donors.Donor`` and ``donors.SystemDonor`` expose the same
    person-level field names (title, first_name, etc.), so a single
    formatter handles either model.
    """
    if not campaign_names:
        campaign_display = "-"
    elif len(campaign_names) == 1:
        campaign_display = next(iter(campaign_names))
    else:
        campaign_display = "Multiple"

    dob = donor.date_of_birth
    return [
        donor.title or "-",
        donor.first_name or "-",
        donor.last_name or "-",
        donor.email or "-",
        donor.phone or "-",
        donor.address_line1 or "-",
        donor.address_line2 or "-",
        donor.city or "-",
        donor.county or "-",
        donor.postcode or "-",
        donor.country or "-",
        dob.strftime("%d/%m/%Y") if dob else "-",
        donor.created_at.strftime("%d/%m/%Y %H:%M") if donor.created_at else "-",
        campaign_display,
    ]


def _generate_unmatched_donors_report(
    *,
    date_from: Any = None,
    date_to: Any = None,
    client_id: str = "",
) -> list[list[str]]:
    """Generate unmatched donors report.

    Surfaces donors awaiting verification from two intake paths:

    * ``donors.Donor`` rows with ``verification_status="pending_export"``,
      created during admin donation entry for house-file campaigns when
      the operator marked ``is_new_donor=True`` — URN to be exported back
      to the charity.
    * ``donors.SystemDonor`` rows with ``pending_review=True``, created
      by scan OCR auto-matching (``scans.scan_processing_donors``) and by
      phone-intake inline donor creation (``donations.intake``).

    Each row is a distinct review-queue item; we do not dedupe across
    models because the two states represent separate operator workflows.

    Args:
        date_from: Optional start date filter applied to the ``created_at``
            timestamp of either source model.
        date_to: Optional end date filter applied to the ``created_at``
            timestamp of either source model.
        client_id: Optional client UUID string. When supplied the result
            is restricted to that charity's donors only. Omit (or pass
            ``""``) to return all charities' unmatched donors (admin /
            superuser use).

    Returns:
        List of row data matching the ``unmatched_donors`` headers in
        ``REPORT_TYPES``. Rows are ordered by ``created_at`` ascending,
        merged across both source models.
    """
    from donors.models import Donor, SystemDonor

    donor_qs = (
        Donor.objects.filter(verification_status=Donor.VERIFICATION_PENDING_EXPORT)
        .select_related("client")
        .prefetch_related("scan_placeholders__batch__campaign")
    )
    sysdonor_qs = (
        SystemDonor.objects.filter(pending_review=True)
        .select_related("client")
        .prefetch_related("donations__campaign")
    )

    if client_id:
        donor_qs = donor_qs.filter(client_id=client_id)
        sysdonor_qs = sysdonor_qs.filter(client_id=client_id)
    if date_from:
        donor_qs = donor_qs.filter(created_at__date__gte=date_from)
        sysdonor_qs = sysdonor_qs.filter(created_at__date__gte=date_from)
    if date_to:
        donor_qs = donor_qs.filter(created_at__date__lte=date_to)
        sysdonor_qs = sysdonor_qs.filter(created_at__date__lte=date_to)

    keyed_rows: list[tuple[Any, list[str]]] = []
    for donor in donor_qs:
        campaign_names = {
            ph.batch.campaign.name
            for ph in donor.scan_placeholders.all()
            if ph.batch and ph.batch.campaign
        }
        keyed_rows.append(
            (donor.created_at, _format_unmatched_row(donor, campaign_names))
        )
    for sysdonor in sysdonor_qs:
        campaign_names = {
            d.campaign.name for d in sysdonor.donations.all() if d.campaign
        }
        keyed_rows.append(
            (sysdonor.created_at, _format_unmatched_row(sysdonor, campaign_names))
        )

    keyed_rows.sort(key=lambda r: r[0])
    return [row for _, row in keyed_rows]


# ---------------------------------------------------------------------------
# Chart data generators for ApexCharts visualizations
# ---------------------------------------------------------------------------
