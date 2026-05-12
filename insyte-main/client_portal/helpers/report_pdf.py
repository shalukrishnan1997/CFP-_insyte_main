"""PDF export helpers for client portal reports."""

import base64
import io
from typing import Any

from django.http import HttpResponse

from client_portal.helpers.common import get_client_campaign
from clients.models import Client

from .report_context import PortalReportFilters


def build_portal_pdf_rows(
    report_type: str,
    filters: PortalReportFilters,
) -> list[list[Any]]:
    """Run the report generator and add leading row numbers for PDF export."""
    from custom_admin.views.reports.helpers import _get_filtered_donations
    from custom_admin.views.reports.main import _run_report_generator

    donations = _get_filtered_donations(
        filters.date_from,
        filters.date_to,
        filters.client_id,
        filters.campaign_id,
    )
    rows = _run_report_generator(
        report_type,
        donations,
        filters.date_from,
        filters.date_to,
        filters.client_id,
    )
    numbered_rows = [[str(index + 1), *row] for index, row in enumerate(rows)]
    return numbered_rows[:500]


def build_portal_pdf_filter_summary(
    client: Client,
    filters: PortalReportFilters,
) -> str:
    """Return the filter summary line shown near the top of the PDF export."""
    summary_parts = [
        f"Client: {client.name}",
        (
            "Period: "
            f"{filters.date_from.strftime('%d/%m/%Y')}"
            f" - {filters.date_to.strftime('%d/%m/%Y')}"
        ),
    ]
    campaign = get_client_campaign(client, filters.campaign_id)
    if campaign is not None:
        summary_parts.append(f"Campaign: {campaign.name}")
    return " | ".join(summary_parts)


def append_chart_image(story: list[Any], chart_b64: str) -> None:
    """Decode and append the optional report chart image to the PDF story."""
    if not chart_b64:
        return

    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Spacer

    try:
        encoded_image = chart_b64.split(",", 1)[1] if "," in chart_b64 else chart_b64
        image_bytes = base64.b64decode(encoded_image)
    except Exception:
        return

    story.append(Image(io.BytesIO(image_bytes), width=170 * mm, height=90 * mm))
    story.append(Spacer(1, 8 * mm))


def build_pdf_story(
    client: Client,
    report_config: dict[str, Any],
    filters: PortalReportFilters,
    rows: list[list[Any]],
    chart_b64: str,
) -> list[Any]:
    """Build the reportlab story for a portal PDF export."""
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(report_config["title"], styles["Title"]),
        Spacer(1, 4 * mm),
        Paragraph(build_portal_pdf_filter_summary(client, filters), styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    append_chart_image(story, chart_b64)
    story.append(build_pdf_table(report_config, rows))
    return story


def build_pdf_table(report_config: dict[str, Any], rows: list[list[Any]]) -> Any:
    """Build the styled reportlab table used in portal PDF exports."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle

    headers = ["#", *report_config["headers"]]
    table_data = [headers, *rows]
    col_width = (170 * mm) / len(headers)
    table = Table(table_data, colWidths=[col_width] * len(headers), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f3f4f6")],
                ),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("WORDWRAP", (0, 0), (-1, -1), True),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def build_portal_pdf_response(
    client: Client,
    report_config: dict[str, Any],
    filters: PortalReportFilters,
    rows: list[list[Any]],
    chart_b64: str,
) -> HttpResponse:
    """Build the PDF attachment response for a portal report export."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    document.build(build_pdf_story(client, report_config, filters, rows, chart_b64))
    buffer.seek(0)
    safe_name = report_config["title"].replace(" ", "_").lower()
    response = HttpResponse(buffer.read(), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="{safe_name}_{filters.date_from}_to_{filters.date_to}.pdf"'
    )
    return response
