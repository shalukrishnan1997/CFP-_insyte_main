"""Report type configurations and constants.

Contains REPORT_TYPES definitions for all report types,
REPORT_CATEGORIES for grouping report cards by category,
and DecimalEncoder for JSON chart data serialisation.
"""

import json
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Category groupings — used by admin and portal report landing page
# Each entry: label, description, icon_svg snippet key, color classes
# ---------------------------------------------------------------------------
REPORT_CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "financial",
        "label": "Financial",
        "description": "Donation amounts, banking, Gift Aid and payment breakdowns",
        "icon": "currency",
        "color_bg": "bg-green-50",
        "color_icon": "text-green-600",
        "color_border": "border-green-200",
        "reports": [
            "donations",
            "banking",
            "gift_aid",
            "payment_method",
            "credit_card",
            "hgv",
            "lgv",
            "paying_in_slips",
        ],
    },
    {
        "key": "campaign",
        "label": "Campaign",
        "description": "Performance vs target and campaign summaries",
        "icon": "chart",
        "color_bg": "bg-blue-50",
        "color_icon": "text-blue-600",
        "color_border": "border-blue-200",
        "reports": ["roi", "campaign_summary"],
    },
    {
        "key": "donors",
        "label": "Donors",
        "description": "Donor management and exception reports",
        "icon": "users",
        "color_bg": "bg-amber-50",
        "color_icon": "text-amber-600",
        "color_border": "border-amber-200",
        "reports": ["unmatched_donors"],
    },
]

# ---------------------------------------------------------------------------
# Report type configurations
# ---------------------------------------------------------------------------
REPORT_TYPES: dict[str, dict[str, Any]] = {
    "donations": {
        "title": "All Donations Report",
        "short_title": "All Donations",
        "description": "Complete list of all donation records",
        "category": "financial",
        "icon": "donations",
        "color_bg": "bg-blue-50",
        "color_icon": "text-blue-600",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Campaign",
            "Amount",
            "Payment Method",
            "Gift Aid",
        ],
    },
    "payment_method": {
        "title": "Payment Method Report",
        "short_title": "Payment Method",
        "description": "Donations grouped by payment method",
        "category": "financial",
        "icon": "card",
        "color_bg": "bg-teal-50",
        "color_icon": "text-teal-600",
        "headers": [
            "S. No.",
            "Payment Method",
            "Count",
            "Total Amount",
            "Average Amount",
            "Percentage",
        ],
    },
    "credit_card": {
        "title": "Credit Card Report",
        "short_title": "Credit Card",
        "description": "Credit card donations with card details",
        "category": "financial",
        "icon": "credit_card",
        "color_bg": "bg-indigo-50",
        "color_icon": "text-indigo-600",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Campaign",
            "Amount",
            "Card Holder",
            "Card Number",
            "Expiry",
        ],
    },
    "banking": {
        "title": "Banking Report",
        "short_title": "Banking",
        "description": "Bank transfer and direct debit details",
        "category": "financial",
        "icon": "bank",
        "color_bg": "bg-green-50",
        "color_icon": "text-green-600",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Campaign",
            "Amount",
            "Slip Number",
        ],
    },
    "gift_aid": {
        "title": "Gift Aid Report",
        "short_title": "Gift Aid",
        "description": "Donations eligible for Gift Aid claims",
        "category": "financial",
        "icon": "gift_aid",
        "color_bg": "bg-purple-50",
        "color_icon": "text-purple-600",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Address",
            "Postcode",
            "Campaign",
            "Amount",
            "Gift Aid Amount",
        ],
    },
    "roi": {
        "title": "Campaign Performance Report",
        "short_title": "Campaign Performance",
        "description": "Performance vs target analysis",
        "category": "campaign",
        "icon": "chart_bar",
        "color_bg": "bg-blue-50",
        "color_icon": "text-blue-600",
        "headers": [
            "S. No.",
            "Campaign",
            "Total Raised",
            "Donation Count",
            "Average Donation",
            "Target",
            "% of Target",
        ],
    },
    "hgv": {
        "title": "High Gift Value (HGV) Report",
        "short_title": "HGV Report",
        "description": "High Gift Value donations",
        "category": "financial",
        "icon": "star",
        "color_bg": "bg-orange-50",
        "color_icon": "text-orange-500",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Campaign",
            "Amount",
            "Threshold",
            "Above By",
        ],
    },
    "lgv": {
        "title": "Low Gift Value (LGV) Report",
        "short_title": "LGV Report",
        "description": "Low Gift Value donations",
        "category": "financial",
        "icon": "plus_circle",
        "color_bg": "bg-yellow-50",
        "color_icon": "text-yellow-600",
        "headers": [
            "S. No.",
            "Date",
            "Donor Name",
            "URN",
            "Campaign",
            "Amount",
            "Threshold",
            "Below By",
        ],
    },
    "campaign_summary": {
        "title": "Campaign Summary Report",
        "short_title": "Campaign Summary",
        "description": "Per-campaign donation totals and Gift Aid",
        "category": "campaign",
        "icon": "clipboard",
        "color_bg": "bg-cyan-50",
        "color_icon": "text-cyan-600",
        "headers": [
            "S. No.",
            "Campaign",
            "Client",
            "Total Amount",
            "Donation Count",
            "Average",
            "Gift Aid Total",
        ],
    },
    "paying_in_slips": {
        "title": "Paying-In Slips Report",
        "short_title": "Paying-In Slips",
        "description": "Daily banking slips with grouped payments",
        "category": "financial",
        "icon": "slip",
        "color_bg": "bg-lime-50",
        "color_icon": "text-lime-600",
        "headers": [
            "S. No.",
            "Slip Number",
            "Client",
            "Banking Date",
            "Payment Type",
            "Items",
            "Total Amount",
            "Status",
            "Created By",
        ],
    },
    "unmatched_donors": {
        "title": "Unmatched Donors Report",
        "short_title": "Unmatched Donors",
        "description": (
            "Donors awaiting verification — captured during scan or phone "
            "intake when no house-file or data-file match was found."
        ),
        "category": "donors",
        "icon": "users",
        "color_bg": "bg-amber-50",
        "color_icon": "text-amber-600",
        "headers": [
            "S. No.",
            "Title",
            "First Name",
            "Last Name",
            "Email",
            "Phone",
            "Address Line 1",
            "Address Line 2",
            "City",
            "County",
            "Postcode",
            "Country",
            "Date of Birth",
            "Date Created",
            "Campaign",
        ],
    },
}


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that converts Decimal values to float for chart data."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)
