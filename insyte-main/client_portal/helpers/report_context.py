"""Report context and filter helpers for client portal views."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.http import HttpRequest

from client_portal.helpers.common import get_client_campaigns
from clients.models import Client
from core.constants import CURRENCY_SYMBOL


@dataclass(frozen=True)
class PortalReportFilters:
    """Normalized filter values for client portal report views."""

    client_id: str
    campaign_id: str
    date_from: date
    date_to: date
    page: str
    per_page: int


def parse_bounded_int(
    raw_value: Any,
    *,
    default: int,
    minimum: int = 1,
    maximum: int = 500,
) -> int:
    """Parse an integer request parameter constrained to a bounded range."""
    try:
        return max(minimum, min(maximum, int(raw_value or default)))
    except TypeError, ValueError:
        return default


def build_portal_report_filters(
    data: Mapping[str, Any],
    client: Client,
) -> PortalReportFilters:
    """Normalize request data for the portal report endpoints."""
    from custom_admin.views.reports.helpers import _parse_date_param

    campaign_id = str(data.get("campaign_id") or data.get("campaign") or "")
    return PortalReportFilters(
        client_id=str(client.id),
        campaign_id=campaign_id,
        date_from=_parse_date_param(data.get("date_from"), default_offset_days=30),
        date_to=_parse_date_param(data.get("date_to")),
        page=str(data.get("page") or "1"),
        per_page=parse_bounded_int(data.get("per_page"), default=25),
    )


def build_filter_panel_context(
    client: Client,
    report_type: str,
    report_config: dict[str, Any],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the context for the HTMX report filter panel."""
    return {
        "report_type": report_type,
        "report_config": report_config,
        "report_title": report_config["title"],
        "report_description": report_config["description"],
        "all_campaigns": get_client_campaigns(client),
        "currency_symbol": CURRENCY_SYMBOL,
        "campaign_id": str(data.get("campaign_id") or data.get("campaign") or ""),
        "date_from": data.get("date_from", ""),
        "date_to": data.get("date_to", ""),
        "per_page": parse_bounded_int(data.get("per_page"), default=25),
        "client_id": str(client.id),
        "scope": "client",
    }


def build_results_panel_context(
    report_context: Mapping[str, Any],
    report_type: str,
    filters: PortalReportFilters,
) -> dict[str, Any]:
    """Merge shared report result state into the rendered partial context."""
    return {
        **report_context,
        "report_type": report_type,
        "client_id": filters.client_id,
        "campaign_id": filters.campaign_id,
        "scope": "client",
        "date_from": filters.date_from,
        "date_to": filters.date_to,
        "per_page": filters.per_page,
        "currency_symbol": CURRENCY_SYMBOL,
        "portal": True,
    }


def parse_portal_pdf_body(request: HttpRequest) -> dict[str, Any]:
    """Parse the JSON body for portal PDF export requests."""
    return json.loads(request.body)
