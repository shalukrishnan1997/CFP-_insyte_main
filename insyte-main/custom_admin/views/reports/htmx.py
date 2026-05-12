"""HTMX partial views for the admin reports dashboard.

Views:
    admin_reports_filter_partial: Returns the filter panel for a given report type.
    admin_reports_results_partial: Returns the chart + table results partial.
    report_export_pdf: Generates and downloads a PDF containing chart + table.
"""

import base64
import io
import json
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from campaigns.models import Campaign
from clients.models import Client
from core.constants import CURRENCY_SYMBOL
from responsehandling.permissions import is_authenticated_and_is_staff

from .constants import REPORT_CATEGORIES, REPORT_TYPES
from .helpers import _get_filtered_donations, _parse_date_param
from .main import (
    _build_campaigns_json,
    _build_report_context,
    _run_report_generator,
)


def _build_category_context() -> dict[str, Any]:
    """Build the category + report cards context for the landing page.

    Returns:
        Dict with 'categories' list and 'report_cards' lookup by type key.
    """
    categories_with_reports: list[dict[str, Any]] = []
    for cat in REPORT_CATEGORIES:
        reports_in_cat = [
            {
                "key": key,
                **REPORT_TYPES[key],
            }
            for key in cat["reports"]
            if key in REPORT_TYPES
        ]
        categories_with_reports.append(
            {
                **cat,
                "report_list": reports_in_cat,
            }
        )
    return {"categories": categories_with_reports}


@is_authenticated_and_is_staff
@require_GET
def admin_reports_filter_partial(
    request: HttpRequest, report_type: str
) -> HttpResponse:
    """Return the filter panel partial for a given report type.

    Args:
        request: Authenticated staff HTTP request.
        report_type: One of the keys from REPORT_TYPES.

    Returns:
        Rendered filter panel HTML partial.
    """
    if report_type not in REPORT_TYPES:
        return HttpResponse("Invalid report type", status=400)

    config = REPORT_TYPES[report_type]
    all_clients = Client.objects.filter(is_active=True).order_by("name")
    all_campaigns = Campaign.objects.filter(client__is_active=True).order_by("name")

    context: dict[str, Any] = {
        "report_type": report_type,
        "report_title": config["title"],
        "report_description": config["description"],
        "all_clients": all_clients,
        "all_campaigns": all_campaigns,
        "all_clients_json": json.dumps(
            [{"id": str(c.id), "name": c.name} for c in all_clients]
        ),
        "all_campaigns_json": json.dumps(_build_campaigns_json(all_campaigns)),
        "currency_symbol": CURRENCY_SYMBOL,
        # Pre-populate from GET params (restoring state after browser back)
        "scope": request.GET.get("scope", "overall"),
        "client_id": request.GET.get("client", ""),
        "campaign_id": request.GET.get("campaign", ""),
        "date_from": _parse_date_param(
            request.GET.get("date_from"), default_offset_days=30
        ),
        "date_to": _parse_date_param(request.GET.get("date_to")),
        "per_page": int(request.GET.get("per_page", "25")),
    }
    return render(request, "admin/reports/partials/filter_panel.html", context)


@is_authenticated_and_is_staff
def admin_reports_results_partial(request: HttpRequest) -> HttpResponse:
    """Return the chart + table results partial.

    Accepts GET or POST parameters:
        report_type, scope, client, campaign, date_from, date_to, page, per_page.

    Args:
        request: Authenticated staff HTTP request.

    Returns:
        Rendered results partial with chart JSON and paginated table rows.
    """
    params = request.POST if request.method == "POST" else request.GET

    report_type = params.get("report_type", "donations")
    if report_type not in REPORT_TYPES:
        return HttpResponse("Invalid report type", status=400)

    scope = params.get("scope", "overall")
    client_id = params.get("client", "")
    campaign_id = params.get("campaign", "")
    date_from = _parse_date_param(params.get("date_from"), default_offset_days=30)
    date_to = _parse_date_param(params.get("date_to"))
    page = params.get("page", "1")
    per_page = int(params.get("per_page", "25"))

    context = _build_report_context(
        request,
        report_type,
        date_from,
        date_to,
        client_id,
        campaign_id,
        scope,
        page,
        per_page,
    )
    context.update(
        {
            "report_type": report_type,
            "scope": scope,
            "client_id": client_id,
            "campaign_id": campaign_id,
            "date_from": date_from,
            "date_to": date_to,
            "per_page": per_page,
            "currency_symbol": CURRENCY_SYMBOL,
            # Leaflet is only needed for the All Donations dashboard map widget.
            # This flag lets the partial conditionally inject the CDN tags when
            # serving via HTMX (the full-page path loads Leaflet in extra_head).
            "needs_leaflet": report_type == "donations",
        }
    )
    return render(request, "admin/reports/partials/results_panel.html", context)


@is_authenticated_and_is_staff
@require_POST
def report_export_pdf(request: HttpRequest) -> HttpResponse:
    """Generate a PDF containing the chart image and data table.

    Accepts a JSON body with:
        report_type (str): The report being exported.
        chart_image_b64 (str): Base64-encoded PNG from ApexCharts dataURI.
        scope (str): overall/client/campaign.
        client (str): Client UUID filter.
        campaign (str): Campaign UUID filter.
        date_from (str): ISO date string.
        date_to (str): ISO date string.

    Args:
        request: Authenticated staff HTTP request with JSON body.

    Returns:
        PDF file download response.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            Image,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError:
        return HttpResponse("ReportLab is required for PDF export.", status=500)

    try:
        body: dict[str, Any] = json.loads(request.body)
    except json.JSONDecodeError, ValueError:
        return HttpResponse("Invalid JSON body.", status=400)

    report_type = body.get("report_type", "donations")
    if report_type not in REPORT_TYPES:
        return HttpResponse("Invalid report type.", status=400)

    chart_image_b64: str = body.get("chart_image_b64", "")
    scope = body.get("scope", "overall")
    client_id = body.get("client", "")
    campaign_id = body.get("campaign", "")
    date_from = _parse_date_param(body.get("date_from"), default_offset_days=30)
    date_to = _parse_date_param(body.get("date_to"))

    config = REPORT_TYPES[report_type]
    donations = _get_filtered_donations(date_from, date_to, client_id, campaign_id)
    report_data = _run_report_generator(
        report_type, donations, date_from, date_to, client_id
    )
    for i, row in enumerate(report_data):
        row.insert(0, str(i + 1))

    buffer = io.BytesIO()
    page_width, _ = A4
    margin = 1.8 * cm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=margin,
        leftMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h1.textColor = colors.HexColor("#1e40af")
    normal = styles["Normal"]
    small = styles["Normal"].clone("Small")
    small.fontSize = 8

    elements: list[Any] = []

    # --- Title ---
    elements.append(Paragraph(config["title"], h1))
    elements.append(Spacer(1, 0.3 * cm))

    # --- Filter summary ---
    filters_text = _build_pdf_filter_summary(
        scope, client_id, campaign_id, date_from, date_to
    )
    elements.append(Paragraph(filters_text, normal))
    elements.append(Spacer(1, 0.5 * cm))

    # --- Chart image ---
    if chart_image_b64:
        try:
            # strip data URI prefix if present
            if "," in chart_image_b64:
                chart_image_b64 = chart_image_b64.split(",", 1)[1]
            img_bytes = base64.b64decode(chart_image_b64)
            img_io = io.BytesIO(img_bytes)
            max_w = page_width - 2 * margin
            chart_img = Image(img_io, width=max_w, height=max_w * 0.45)
            elements.append(chart_img)
            elements.append(Spacer(1, 0.6 * cm))
        except Exception:
            pass  # skip chart if decoding fails

    # --- Table ---
    headers = config["headers"]
    if report_data:
        table_data: list[list[str]] = [headers, *report_data[:500]]
        col_count = len(headers)
        available_width = page_width - 2 * margin
        col_width = available_width / col_count

        tbl = Table(
            table_data,
            colWidths=[col_width] * col_count,
            repeatRows=1,
        )
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f9fafb")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        elements.append(tbl)
        if len(report_data) > 500:
            elements.append(Spacer(1, 0.3 * cm))
            elements.append(
                Paragraph(
                    f"Table limited to 500 rows. Full dataset contains {len(report_data)} records.",
                    small,
                )
            )
    else:
        elements.append(
            Paragraph("No data available for the selected filters.", normal)
        )

    doc.build(elements)
    buffer.seek(0)

    filename = f"{report_type}_report_{date_from}_to_{date_to}.pdf"
    response = HttpResponse(buffer.read(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _build_pdf_filter_summary(
    scope: str,
    client_id: str,
    campaign_id: str,
    date_from: Any,
    date_to: Any,
) -> str:
    """Build a human-readable filter summary string for the PDF header.

    Args:
        scope: Report scope (overall/client/campaign).
        client_id: Client UUID string (may be empty).
        campaign_id: Campaign UUID string (may be empty).
        date_from: Start date object or string.
        date_to: End date object or string.

    Returns:
        Formatted string describing the active filters.
    """
    parts = []
    if date_from:
        parts.append(f"From: {date_from}")
    if date_to:
        parts.append(f"To: {date_to}")
    if client_id:
        try:
            c = Client.objects.get(pk=client_id)
            parts.append(f"Client: {c.name}")
        except Client.DoesNotExist:
            parts.append(f"Client: {client_id}")
    if campaign_id:
        try:
            camp = Campaign.objects.get(pk=campaign_id)
            parts.append(f"Campaign: {camp.name}")
        except Campaign.DoesNotExist:
            parts.append(f"Campaign: {campaign_id}")
    if scope and scope != "overall":
        parts.append(f"Scope: {scope.title()}")
    return " | ".join(parts) if parts else "All data"
