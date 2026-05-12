"""URL configuration for client portal.

Routes for the client-facing portal where clients can view their
campaign reports and statistics.
"""

from django.urls import path

from client_portal import views

app_name = "client_portal"

urlpatterns = [
    path("", views.client_dashboard, name="dashboard"),
    path("reports/", views.client_reports, name="reports"),
    path(
        "reports/export/",
        views.client_report_export,
        name="report_export",
    ),
    path(
        "reports/export/pdf/",
        views.client_report_export_pdf,
        name="report_export_pdf",
    ),
    path(
        "reports/htmx/filters/<str:report_type>/",
        views.client_reports_filter_partial,
        name="reports_filter_partial",
    ),
    path(
        "reports/htmx/results/",
        views.client_reports_results_partial,
        name="reports_results_partial",
    ),
    path(
        "supporters/",
        views.client_supporters,
        name="client_supporters",
    ),
    path(
        "supporters/<str:urn>/",
        views.client_supporter_detail,
        name="client_supporter_detail",
    ),
    path(
        "supporters/data-file/<str:pk>/",
        views.client_supporter_detail_data_file,
        name="client_supporter_detail_data_file",
    ),
    path(
        "donations/<str:donation_id>/scan-form/",
        views.client_scan_form_view,
        name="scan_form_view",
    ),
]
