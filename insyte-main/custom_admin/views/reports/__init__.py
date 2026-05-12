"""Reports package — split from the original 2188-line reports.py.

Re-exports the two public view functions so existing URL config
can continue using ``from custom_admin.views.reports import admin_reports``.
Also exports _get_campaign_widgets for campaign dashboard use.
"""

from custom_admin.views.reports.charts import _get_campaign_widgets
from custom_admin.views.reports.constants import REPORT_CATEGORIES, REPORT_TYPES
from custom_admin.views.reports.htmx import (
    admin_reports_filter_partial,
    admin_reports_results_partial,
    report_export_pdf,
)
from custom_admin.views.reports.main import admin_reports, report_export
from custom_admin.views.reports.scans_zip import (
    report_scans_zip,
    report_supports_scan_zip,
)

__all__ = [
    "REPORT_CATEGORIES",
    "REPORT_TYPES",
    "_get_campaign_widgets",
    "admin_reports",
    "admin_reports_filter_partial",
    "admin_reports_results_partial",
    "report_export",
    "report_export_pdf",
    "report_scans_zip",
    "report_supports_scan_zip",
]
