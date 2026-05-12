"""Shared helper functions for report generation.

Functions:
    _parse_date_param: Parse date strings with UK format support.
    _get_filtered_donations: Build filtered donation querysets.
    _get_donor_name: Get display name from donation record.
    _get_donor_urn: Get donor URN string.
    _get_donor_address: Get donor address and postcode.
    _get_uk_region_for_postcode: Map UK postcode to NUTS1 region.
"""

from typing import Any

from django.db.models import Q

from core.date_utils import parse_date_with_default
from donations.models import Donation


def _parse_date_param(date_str: str | None, default_offset_days: int = 0) -> Any:
    """Parse a date string parameter with multiple format support.

    Args:
        date_str: Date string to parse.
        default_offset_days: Days to offset from today if no date provided.

    Returns:
        Parsed date object.
    """
    return parse_date_with_default(date_str, default_offset_days=-default_offset_days)


def _get_filtered_donations(
    date_from: Any,
    date_to: Any,
    client_id: str | None = None,
    campaign_id: str | None = None,
) -> Any:
    """Get filtered donation queryset.

    Filters by donation_date if available, or by created_at if donation_date is null.
    """
    donations = Donation.objects.select_related(
        "campaign",
        "campaign__client",
        "donor",
        "data_file_donor",
    ).filter(
        # Include donations with donation_date in range OR null donation_date with created_at in range
        Q(donation_date__gte=date_from, donation_date__lte=date_to)
        | Q(
            donation_date__isnull=True,
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
    )

    if client_id:
        donations = donations.filter(campaign__client_id=client_id)

    if campaign_id:
        donations = donations.filter(campaign_id=campaign_id)

    return donations.order_by("-donation_date")


def _get_donor_name(donation: Donation) -> str:
    """Get donor name from donation record."""
    if donation.donor:
        return f"{donation.donor.title or ''} {donation.donor.first_name} {donation.donor.last_name}".strip()
    if donation.data_file_donor:
        dfd = donation.data_file_donor
        return f"{dfd.title or ''} {dfd.first_name} {dfd.last_name}".strip()
    return donation.caf_donor_name or "Unknown"


def _get_donor_urn(donation: Donation) -> str:
    """Get donor URN from donation record."""
    if donation.donor:
        return str(donation.donor.urn) if donation.donor.urn else "-"
    if donation.data_file_donor:
        return (
            str(donation.data_file_donor.urn) if donation.data_file_donor.urn else "-"
        )
    return "-"


def _get_donor_address(donation: Donation) -> tuple[str, str]:
    """Get donor address and postcode from donation record."""
    if donation.donor:
        addr = ", ".join(
            filter(
                None,
                [
                    donation.donor.address_line1,
                    donation.donor.city,
                    donation.donor.county,
                ],
            )
        )
        return addr, donation.donor.postcode or ""
    if donation.data_file_donor:
        dfd = donation.data_file_donor
        addr = ", ".join(
            filter(
                None,
                [
                    dfd.address_line1,
                    dfd.city,
                    dfd.county,
                ],
            )
        )
        return addr, dfd.postcode or ""
    return "", ""


def _get_uk_region_for_postcode(postcode: str) -> str:
    """Map UK postcode outcode to NUTS1 region names."""
    if not postcode:
        return "Unknown"

    outcode = postcode.strip().split(" ")[0].upper()
    # Basic mapping of major outcode prefixes to regions
    mapping = {
        # London
        "E": "London",
        "EC": "London",
        "N": "London",
        "NW": "London",
        "SE": "London",
        "SW": "London",
        "W": "London",
        "WC": "London",
        # South East
        "BN": "South East",
        "CT": "South East",
        "GU": "South East",
        "HP": "South East",
        "MK": "South East",
        "OX": "South East",
        "PO": "South East",
        "RG": "South East",
        "RH": "South East",
        "SL": "South East",
        "SO": "South East",
        "TN": "South East",
        "ME": "South East",
        # South West
        "BA": "South West",
        "BH": "South West",
        "BS": "South West",
        "DT": "South West",
        "EX": "South West",
        "GL": "South West",
        "PL": "South West",
        "SN": "South West",
        "SP": "South West",
        "TA": "South West",
        "TQ": "South West",
        "TR": "South West",
        # West Midlands
        "B": "West Midlands",
        "CV": "West Midlands",
        "DY": "West Midlands",
        "HR": "West Midlands",
        "ST": "West Midlands",
        "TF": "West Midlands",
        "WR": "West Midlands",
        "WS": "West Midlands",
        "WV": "West Midlands",
        # East Midlands
        "DE": "East Midlands",
        "DN": "East Midlands",
        "LE": "East Midlands",
        "LN": "East Midlands",
        "NG": "East Midlands",
        "NN": "East Midlands",
        # East of England
        "AL": "East of England",
        "CB": "East of England",
        "CM": "East of England",
        "CO": "East of England",
        "IP": "East of England",
        "LU": "East of England",
        "NR": "East of England",
        "SG": "East of England",
        "SS": "East of England",
        # North West
        "BB": "North West",
        "BL": "North West",
        "CA": "North West",
        "CH": "North West",
        "CW": "North West",
        "FY": "North West",
        "L": "North West",
        "LA": "North West",
        "M": "North West",
        "OL": "North West",
        "PR": "North West",
        "SK": "North West",
        "WA": "North West",
        "WN": "North West",
        # North East
        "DH": "North East",
        "DL": "North East",
        "NE": "North East",
        "SR": "North East",
        "TS": "North East",
        # Yorkshire and the Humber
        "BD": "Yorkshire and the Humber",
        "HD": "Yorkshire and the Humber",
        "HG": "Yorkshire and the Humber",
        "HU": "Yorkshire and the Humber",
        "HX": "Yorkshire and the Humber",
        "LS": "Yorkshire and the Humber",
        "S": "Yorkshire and the Humber",
        "WF": "Yorkshire and the Humber",
        "YO": "Yorkshire and the Humber",
        # Scotland
        "AB": "Scotland",
        "DD": "Scotland",
        "DG": "Scotland",
        "EH": "Scotland",
        "FK": "Scotland",
        "G": "Scotland",
        "HS": "Scotland",
        "IV": "Scotland",
        "KA": "Scotland",
        "KW": "Scotland",
        "KY": "Scotland",
        "ML": "Scotland",
        "PA": "Scotland",
        "PH": "Scotland",
        "TD": "Scotland",
        "ZE": "Scotland",
        # Wales
        "CF": "Wales",
        "LD": "Wales",
        "LL": "Wales",
        "NP": "Wales",
        "SA": "Wales",
        "SY": "Wales",
        # Northern Ireland
        "BT": "Northern Ireland",
    }

    # Check for 2-letter prefix first
    prefix2 = outcode[:2]
    if prefix2 in mapping:
        return mapping[prefix2]

    # Check for 1-letter prefix
    prefix1 = outcode[:1]
    if prefix1 in mapping:
        return mapping[prefix1]

    return "Unknown"
