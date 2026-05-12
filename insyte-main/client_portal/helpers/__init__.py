"""Public helper exports for client portal views.

Import from this package in callers. Names re-exported here are the stable
public API for portal view code; individual submodules remain internal
implementation details and may be reorganized without changing callers.
"""

from client_portal.helpers.common import (
    build_client_report_categories,
    campaign_belongs_to_client,
    get_client_campaigns,
    get_client_report_config,
    get_portal_client,
)
from client_portal.helpers.dashboard import build_client_dashboard_context
from client_portal.helpers.report_context import (
    build_filter_panel_context,
    build_portal_report_filters,
    build_results_panel_context,
    parse_portal_pdf_body,
)
from client_portal.helpers.report_csv import (
    build_donation_export_response,
    build_unmatched_donors_export_response,
)
from client_portal.helpers.report_pdf import (
    build_portal_pdf_response,
    build_portal_pdf_rows,
)
from client_portal.helpers.supporters import (
    build_portal_supporter_filters,
    build_supporter_detail_context,
    build_supporter_list_context,
    get_scoped_supporter_or_404,
    get_supporter_donations,
)

__all__ = [
    "build_client_dashboard_context",
    "build_client_report_categories",
    "build_donation_export_response",
    "build_filter_panel_context",
    "build_portal_pdf_response",
    "build_portal_pdf_rows",
    "build_portal_report_filters",
    "build_portal_supporter_filters",
    "build_results_panel_context",
    "build_supporter_detail_context",
    "build_supporter_list_context",
    "build_unmatched_donors_export_response",
    "campaign_belongs_to_client",
    "get_client_campaigns",
    "get_client_report_config",
    "get_portal_client",
    "get_scoped_supporter_or_404",
    "get_supporter_donations",
    "parse_portal_pdf_body",
]
