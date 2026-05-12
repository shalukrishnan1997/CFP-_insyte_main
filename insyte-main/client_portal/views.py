"""Client Portal views for donation management system.

This module contains view functions for the client portal interface,
where clients can view reports, statistics, and supporter information
for their own campaigns only.

All views require authentication and enforce row-level security by client.
"""

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from client_portal.helpers import (
    build_client_dashboard_context,
    build_client_report_categories,
    build_donation_export_response,
    build_filter_panel_context,
    build_portal_pdf_response,
    build_portal_pdf_rows,
    build_portal_report_filters,
    build_portal_supporter_filters,
    build_results_panel_context,
    build_supporter_detail_context,
    build_supporter_list_context,
    build_unmatched_donors_export_response,
    campaign_belongs_to_client,
    get_client_campaigns,
    get_client_report_config,
    get_portal_client,
    get_scoped_supporter_or_404,
    get_supporter_donations,
    parse_portal_pdf_body,
)
from core.constants import CURRENCY_SYMBOL
from donors.models import DataFileDonor, Donor


def client_required(
    view_func: Callable[..., HttpResponse],
) -> Callable[..., HttpResponse]:
    """Decorator to ensure user has a client portal profile.

    Args:
        view_func: The view function to wrap

    Returns:
        Wrapped view function that checks for client portal profile
    """

    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        profile = getattr(request.user, "client_portal_profile", None)
        if not profile or not profile.is_active:
            return redirect("auth_app:login")
        if not profile.client.is_active:
            return redirect("auth_app:login")
        return view_func(request, *args, **kwargs)

    return wraps(view_func)(wrapper)


@login_required
@client_required
def client_dashboard(request: HttpRequest) -> HttpResponse:
    """Client portal dashboard with campaign overview.

    Shows a summary of the client's campaigns and basic statistics.
    Only displays data for the logged-in client's campaigns.

    Args:
        request: HTTP request from authenticated client user.

    Returns:
        Rendered dashboard page with client's campaign data.
    """
    client = get_portal_client(request)
    return render(
        request,
        "client_portal/dashboard.html",
        build_client_dashboard_context(client),
    )


@login_required
@client_required
def client_reports(request: HttpRequest) -> HttpResponse:
    """Reports landing page for the client portal.

    Displays categorised report cards scoped to the portal user's client.
    Report selection, filter configuration, and results are loaded via
    HTMX partials to keep the page lightweight.

    Args:
        request: HTTP request from authenticated client user.

    Returns:
        Rendered reports landing page with category cards.
    """
    client = get_portal_client(request)

    context: dict[str, Any] = {
        "client": client,
        "categories": build_client_report_categories(),
        "all_campaigns": get_client_campaigns(client),
        "active": "reports",
        "currency_symbol": CURRENCY_SYMBOL,
    }
    return render(request, "client_portal/reports/index.html", context)


@login_required
@client_required
def client_report_export(request: HttpRequest) -> HttpResponse:
    """Export reports to CSV format for client.

    Generates CSV exports of donations data with campaign and donor information.
    Respects date range and campaign filters from the reports page.
    Only exports data for the logged-in client's campaigns.

    Args:
        request: HTTP request with GET parameters for filtering.

    Returns:
        CSV file download response with detailed donation data for client's campaigns.
    """
    client = get_portal_client(request)
    report_type = request.GET.get("report_type", "donations")
    filters = build_portal_report_filters(request.GET, client)

    # ------------------------------------------------------------------
    # Unmatched donors report — delegates to the shared generator so the
    # CSV matches what is shown in the portal results table exactly.
    # ------------------------------------------------------------------
    if report_type == "unmatched_donors":
        return build_unmatched_donors_export_response(client, filters)

    return build_donation_export_response(client, filters)


@login_required
@client_required
def client_reports_filter_partial(
    request: HttpRequest, report_type: str
) -> HttpResponse:
    """HTMX partial: filter panel for a specific report type.

    Returns the filter form HTML fragment for the given report type,
    pre-populated with campaign choices scoped to the portal user's client.
    No scope or client selectors are shown (always locked to this client).

    Args:
        request: HTTP request.
        report_type: Report type key (e.g. ``"donations"``, ``"banking"``).

    Returns:
        Rendered filter panel partial, or 404 if report type is invalid.
    """
    client = get_portal_client(request)
    report_config = get_client_report_config(report_type)
    if report_config is None:
        return HttpResponse(status=404)

    context = build_filter_panel_context(
        client, report_type, report_config, request.GET
    )
    return render(request, "client_portal/reports/partials/filter_panel.html", context)


@login_required
@client_required
def client_reports_results_partial(request: HttpRequest) -> HttpResponse:
    """HTMX partial: charts and data table for a client portal report.

    Runs the report generator, builds chart data, paginates rows, and returns
    the combined chart + table fragment. Campaign ownership is validated
    against the portal user's client to prevent cross-client data access.

    Args:
        request: HTTP request (GET or POST) with report parameters.

    Returns:
        Rendered results partial with chart and paginated table.
    """
    from custom_admin.views.reports.main import _build_report_context

    client = get_portal_client(request)

    data = request.POST if request.method == "POST" else request.GET
    report_type = data.get("report_type", "")

    if not report_type or get_client_report_config(report_type) is None:
        return render(
            request,
            "client_portal/reports/partials/results_panel.html",
            {"error": "Please select a valid report type."},
        )

    filters = build_portal_report_filters(data, client)

    # Security: validate any supplied campaign belongs to this client
    if filters.campaign_id and not campaign_belongs_to_client(
        client, filters.campaign_id
    ):
        return HttpResponse("Forbidden", status=403)

    try:
        report_context = _build_report_context(
            request=request,
            report_type=report_type,
            date_from=filters.date_from,
            date_to=filters.date_to,
            client_id=filters.client_id,
            campaign_id=filters.campaign_id,
            scope="client",
            page=filters.page,
            per_page=filters.per_page,
        )
    except Exception as exc:
        return render(
            request,
            "client_portal/reports/partials/results_panel.html",
            {"error": f"Failed to generate report: {exc}"},
        )

    context = build_results_panel_context(report_context, report_type, filters)
    return render(request, "client_portal/reports/partials/results_panel.html", context)


@login_required
@client_required
def client_report_export_pdf(request: HttpRequest) -> HttpResponse:
    """Export a report as PDF (chart image + data table).

    Accepts a POST body with the report parameters and an optional base-64
    encoded PNG chart image captured from ApexCharts.  Generates an A4
    PDF containing a header, filter summary, chart (if provided), and a
    data table (capped at 500 rows).  Campaign ownership is validated
    against the portal user's client.

    Args:
        request: HTTP POST request with JSON body:
            ``report_type``, ``chart_image_b64`` (optional),
            ``date_from``, ``date_to``, ``campaign_id``, ``per_page``.

    Returns:
        ``application/pdf`` response, or JSON error on failure.
    """
    from django.http import JsonResponse

    client = get_portal_client(request)

    try:
        body = parse_portal_pdf_body(request)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    report_type = body.get("report_type", "")
    report_config = get_client_report_config(report_type)
    if not report_type or report_config is None:
        return JsonResponse({"error": "Invalid report type."}, status=400)

    filters = build_portal_report_filters(body, client)
    if filters.campaign_id and not campaign_belongs_to_client(
        client, filters.campaign_id
    ):
        return HttpResponse("Forbidden", status=403)

    rows = build_portal_pdf_rows(report_type, filters)
    return build_portal_pdf_response(
        client,
        report_config,
        filters,
        rows,
        str(body.get("chart_image_b64", "")),
    )


@login_required
@client_required
def client_supporters(request: HttpRequest) -> HttpResponse:
    """Searchable supporter list for a client's campaigns.

    Defaults to all campaigns; an optional campaign filter narrows results.
    Each row shows the donor plus aggregate totals scoped to the client.
    Includes both house-file (Donor) and data-file (DataFileDonor) supporters.

    Args:
        request: HTTP request with optional GET parameters:
            search: Free-text search (URN, name, email, postcode).
            campaign: Campaign UUID to filter by.
            page: Page number.

    Returns:
        Rendered supporter list page for the client portal.
    """
    client = get_portal_client(request)
    filters = build_portal_supporter_filters(request)
    return render(
        request,
        "client_portal/supporters.html",
        build_supporter_list_context(request, client=client, filters=filters),
    )


@login_required
@client_required
def client_supporter_detail(request: HttpRequest, urn: str) -> HttpResponse:
    """Detail page for a single supporter within the client's campaigns.

    Shows full profile, consent/GDPR flags, and donation history scoped
    to the client's campaigns. Returns 404 if the donor has no donations
    under this client (prevents cross-client data leakage).

    Args:
        request: HTTP request.
        urn: Donor URN (primary key).

    Returns:
        Rendered supporter detail page for the client portal.
    """
    client = get_portal_client(request)
    donor = get_scoped_supporter_or_404(
        client,
        model=Donor,
        lookup_field="urn",
        lookup_value=urn,
    )
    donations_qs = get_supporter_donations(
        client,
        donation_field="donor",
        supporter=donor,
    )
    return render(
        request,
        "client_portal/supporter_detail.html",
        build_supporter_detail_context(
            request,
            client=client,
            donor=donor,
            donations_qs=donations_qs,
        ),
    )


@login_required
@client_required
def client_supporter_detail_data_file(request: HttpRequest, pk: str) -> HttpResponse:
    """Detail page for a data-file supporter within the client's campaigns.

    Shows full profile, consent/GDPR flags, and donation history scoped
    to the client's campaigns. Returns 404 if the donor has no donations
    under this client (prevents cross-client data leakage).

    Args:
        request: HTTP request.
        pk: DataFileDonor primary key (UUID string).

    Returns:
        Rendered supporter detail page for the client portal.
    """
    client = get_portal_client(request)
    donor = get_scoped_supporter_or_404(
        client,
        model=DataFileDonor,
        lookup_field="pk",
        lookup_value=pk,
    )
    donations_qs = get_supporter_donations(
        client,
        donation_field="data_file_donor",
        supporter=donor,
    )
    return render(
        request,
        "client_portal/supporter_detail.html",
        build_supporter_detail_context(
            request,
            client=client,
            donor=donor,
            donations_qs=donations_qs,
        ),
    )


@login_required
@client_required
def client_scan_form_view(request: HttpRequest, donation_id: str) -> HttpResponse:
    """Serve the scanned donation form for a specific donation.

    Renders the canonical page-image viewer for the donation's placeholder.
    Row-level security is enforced: the donation must belong to the
    authenticated client's campaigns.

    Args:
        request: HTTP request from authenticated client portal user.
        donation_id: UUID of the Donation whose scanned form is requested.

    Returns:
        Rendered image-first scan viewer, or 404 on failure.
    """
    from donations.models import Donation
    from scans.donation_scan import DonationScanService

    client = get_portal_client(request)

    try:
        donation = Donation.objects.select_related("scan_placeholder", "campaign").get(
            id=donation_id, campaign__client=client
        )
    except Donation.DoesNotExist as exc:
        raise Http404 from exc

    try:
        placeholder = donation.scan_placeholder  # type: ignore[union-attr]
    except Exception as exc:
        raise Http404 from exc

    page_urls = DonationScanService.get_placeholder_page_urls(
        placeholder,
        user=request.user if request.user.is_authenticated else None,
    )
    if not page_urls:
        raise Http404

    return render(
        request,
        "client_portal/scan_view.html",
        {
            "client": client,
            "donation": donation,
            "placeholder": placeholder,
            "image_url": page_urls[0],
            "page_urls_json": json.dumps(page_urls),
            "has_image": True,
            "active": "supporters",
        },
    )
