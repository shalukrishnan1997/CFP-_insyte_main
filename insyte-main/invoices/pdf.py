"""Invoice PDF generation service using ReportLab.

Provides a unified PDF builder for both download and inline preview modes,
eliminating code duplication between the two use cases.

Uses ReportLab for cross-platform PDF generation with professional styling.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Literal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from invoices.models import Invoice, InvoiceSettings

logger = logging.getLogger(__name__)

# Type alias for line item tuples: (description, quantity, unit_price, line_total)
LineItem = tuple[str, int | float, Decimal, Decimal]

# Supported PDF generation modes
PdfMode = Literal["download", "preview"]


@dataclass(frozen=True)
class _FontConfig:
    """Font size configuration for a PDF generation mode.

    Attributes:
        title: Title heading font size.
        heading: Section heading font size.
        normal: Normal body text font size.
        small: Small/fine-print font size.
        table_header: Items table header font size.
        table_body: Items table body font size.
        summary_normal: Financial summary normal font size.
        summary_bold: Financial summary bold (total/balance) font size.
        info_label: Invoice info label font size.
        info_value: Invoice info value font size.
    """

    title: int
    heading: int
    normal: int
    small: int
    table_header: int
    table_body: int
    summary_normal: int
    summary_bold: int
    info_label: int
    info_value: int


_FONT_CONFIGS: dict[PdfMode, _FontConfig] = {
    "download": _FontConfig(
        title=20,
        heading=12,
        normal=9,
        small=8,
        table_header=10,
        table_body=8,
        summary_normal=10,
        summary_bold=12,
        info_label=9,
        info_value=9,
    ),
    "preview": _FontConfig(
        title=18,
        heading=11,
        normal=8,
        small=7,
        table_header=9,
        table_body=7,
        summary_normal=8,
        summary_bold=10,
        info_label=8,
        info_value=8,
    ),
}


@dataclass(frozen=True)
class _LayoutConfig:
    """Layout spacing/column configuration for a PDF generation mode.

    Attributes:
        header_spacer: Spacer height after company header.
        title_spacer: Spacer height after invoice title.
        info_col_widths: Column widths for invoice info table.
        items_col_widths: Column widths for line items table.
        items_header_labels: Header labels for the line items table.
        summary_col_widths: Column widths for financial summary table.
        section_spacer: Spacer height between major sections.
        show_phone: Whether to show company phone in header.
        show_email: Whether to show company email in header.
        compact_bill_to: Whether to use compact bill-to section.
        show_billing_period: Whether to show billing period in info table.
        title_prefix: Prefix for the invoice title text.
    """

    header_spacer: float
    title_spacer: float
    info_col_widths: list[float]
    items_col_widths: list[float]
    items_header_labels: list[str]
    summary_col_widths: list[float]
    section_spacer: float
    show_phone: bool
    show_email: bool
    compact_bill_to: bool
    show_billing_period: bool
    title_prefix: str


_LAYOUT_CONFIGS: dict[PdfMode, _LayoutConfig] = {
    "download": _LayoutConfig(
        header_spacer=0.25,
        title_spacer=0.15,
        info_col_widths=[1.2, 2.3, 1.0, 2.0],
        items_col_widths=[3.2, 0.8, 1.1, 1.1],
        items_header_labels=["Description", "Qty", "Unit Price", "Total"],
        summary_col_widths=[5.0, 2.0],
        section_spacer=0.2,
        show_phone=True,
        show_email=True,
        compact_bill_to=False,
        show_billing_period=True,
        title_prefix="INVOICE",
    ),
    "preview": _LayoutConfig(
        header_spacer=0.2,
        title_spacer=0.1,
        info_col_widths=[0.8, 2.0, 0.8, 1.5],
        items_col_widths=[3.2, 0.7, 1.0, 1.0],
        items_header_labels=["Service Description", "Qty", "Rate", "Total"],
        summary_col_widths=[4.5, 1.4],
        section_spacer=0.15,
        show_phone=False,
        show_email=False,
        compact_bill_to=True,
        show_billing_period=False,
        title_prefix="INVOICE #",
    ),
}

# Shared colour palette
_BLUE = colors.HexColor("#1e40af")
_DARK = colors.HexColor("#1f2937")
_ZEBRA_GREY = colors.HexColor("#f9fafb")


class InvoicePdfService:
    """Service for generating invoice PDF documents.

    Supports two modes:
    - ``download``: Full-size, higher font sizes, attached as file download.
    - ``preview``: Compact, lower font sizes, displayed inline in the browser.
    """

    @staticmethod
    def build_pdf(
        invoice: Invoice,
        line_items: list[LineItem],
        settings: InvoiceSettings,
        *,
        mode: PdfMode = "download",
    ) -> bytes:
        """Build a PDF document for the given invoice.

        Args:
            invoice: The invoice to render.
            line_items: List of (description, qty, unit_price, total) tuples.
            settings: Company-level invoice settings (name, address, etc.).
            mode: ``"download"`` for full-size attachment, ``"preview"`` for
                compact inline display.

        Returns:
            Raw PDF bytes ready to be written to an ``HttpResponse``.

        Raises:
            ValueError: If *mode* is not ``"download"`` or ``"preview"``.
        """
        if mode not in _FONT_CONFIGS:
            msg = f"Invalid mode '{mode}'. Expected 'download' or 'preview'."
            raise ValueError(msg)

        fonts = _FONT_CONFIGS[mode]
        layout = _LAYOUT_CONFIGS[mode]

        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
        )

        base_styles = getSampleStyleSheet()
        styles = _build_styles(base_styles, fonts)
        elements: list[Flowable] = []

        # --- Company header ---
        _add_company_header(elements, settings, styles, layout, fonts)

        # --- Invoice title ---
        title_text = (
            f"<b>{layout.title_prefix}{invoice.invoice_number}</b>"
            if layout.title_prefix.endswith("#")
            else f"<b>{layout.title_prefix}</b>"
        )
        elements.append(Paragraph(title_text, styles["title"]))
        elements.append(Spacer(1, layout.title_spacer * inch))

        # --- Invoice info table ---
        _add_invoice_info(elements, invoice, styles, layout, fonts)
        elements.append(Spacer(1, layout.section_spacer * inch))

        # --- Bill To ---
        _add_bill_to(elements, invoice, styles, layout)

        # --- Line items table ---
        elements.append(Paragraph("<b>Services Provided</b>", styles["heading"]))
        elements.append(Spacer(1, 0.1 * inch))
        _add_items_table(elements, line_items, styles, layout, fonts)
        elements.append(Spacer(1, layout.header_spacer * inch))

        # --- Financial summary ---
        _add_summary_table(elements, invoice, layout, fonts)

        doc.build(elements)
        pdf_bytes = buf.getvalue()
        buf.close()
        return pdf_bytes

    @staticmethod
    def get_content_disposition(invoice: Invoice, *, mode: PdfMode) -> str:
        """Return the Content-Disposition header value for the given mode.

        Args:
            invoice: Invoice to generate filename for.
            mode: ``"download"`` or ``"preview"``.

        Returns:
            Formatted Content-Disposition header string.
        """
        if mode == "download":
            filename = f"Invoice_{invoice.invoice_number}.pdf"
            return f'attachment; filename="{filename}"'
        filename = f"Invoice_{invoice.invoice_number}_Preview.pdf"
        return f'inline; filename="{filename}"'


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_styles(base: object, fonts: _FontConfig) -> dict[str, ParagraphStyle]:
    """Create the paragraph styles dict from base stylesheet and font config."""
    return {
        "title": ParagraphStyle(
            "CustomTitle",
            parent=base["Heading1"],  # type: ignore[index]
            fontSize=fonts.title,
            textColor=_BLUE,
            spaceAfter=12 if fonts.title >= 20 else 10,
        ),
        "heading": ParagraphStyle(
            "CustomHeading",
            parent=base["Heading2"],  # type: ignore[index]
            fontSize=fonts.heading,
            textColor=_DARK,
            spaceAfter=8 if fonts.heading >= 12 else 6,
        ),
        "normal": ParagraphStyle(
            "CustomNormal",
            parent=base["Normal"],  # type: ignore[index]
            fontSize=fonts.normal,
        ),
        "small": ParagraphStyle(
            "SmallText",
            parent=base["Normal"],  # type: ignore[index]
            fontSize=fonts.small,
        ),
    }


def _add_company_header(
    elements: list[Flowable],
    settings: InvoiceSettings,
    styles: dict[str, ParagraphStyle],
    layout: _LayoutConfig,
    fonts: _FontConfig,
) -> None:
    """Add company name, address, phone, and email to the element list."""
    elements.append(
        Paragraph(
            f"<b>{settings.company_name or 'Response Handling'}</b>",
            styles["heading"],
        )
    )
    if settings.company_address:
        for line in settings.company_address.split("\n"):
            elements.append(Paragraph(line, styles["small"]))
    if layout.show_phone and settings.company_phone:
        elements.append(Paragraph(f"Phone: {settings.company_phone}", styles["small"]))
    if layout.show_email and settings.company_email:
        elements.append(Paragraph(f"Email: {settings.company_email}", styles["small"]))
    elements.append(Spacer(1, layout.header_spacer * inch))


def _add_invoice_info(
    elements: list[Flowable],
    invoice: Invoice,
    styles: dict[str, ParagraphStyle],
    layout: _LayoutConfig,
    fonts: _FontConfig,
) -> None:
    """Add the invoice details info table."""
    col_widths = [w * inch for w in layout.info_col_widths]

    if layout.show_billing_period:
        # Full mode — 3 rows including billing period
        data = [
            [
                "Invoice Number:",
                invoice.invoice_number,
                "Issue Date:",
                invoice.issue_date.strftime("%d/%m/%Y"),
            ],
            [
                "Status:",
                invoice.get_status_display(),
                "Due Date:",
                invoice.due_date.strftime("%d/%m/%Y"),
            ],
            [
                "Billing Period:",
                (
                    f"{invoice.billing_period_start.strftime('%d/%m/%Y')}"
                    f" - {invoice.billing_period_end.strftime('%d/%m/%Y')}"
                ),
                "",
                "",
            ],
        ]
    else:
        # Compact mode — 2 rows
        data = [
            [
                "Invoice:",
                invoice.invoice_number,
                "Issued:",
                invoice.issue_date.strftime("%d/%m/%Y"),
            ],
            [
                "Status:",
                invoice.get_status_display(),
                "Due:",
                invoice.due_date.strftime("%d/%m/%Y"),
            ],
        ]

    table = Table(data, colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (0, -1), "Helvetica-Bold", fonts.info_label),
                ("FONT", (2, 0), (2, -1), "Helvetica-Bold", fonts.info_label),
                ("FONT", (1, 0), (1, -1), "Helvetica", fonts.info_value),
                ("FONT", (3, 0), (3, -1), "Helvetica", fonts.info_value),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(table)


def _add_bill_to(
    elements: list[Flowable],
    invoice: Invoice,
    styles: dict[str, ParagraphStyle],
    layout: _LayoutConfig,
) -> None:
    """Add the 'Bill To' section."""
    if layout.compact_bill_to:
        elements.append(
            Paragraph(f"<b>Bill To: {invoice.client.name}</b>", styles["heading"])
        )
    else:
        elements.append(Paragraph("<b>Bill To:</b>", styles["heading"]))
        elements.append(Paragraph(f"<b>{invoice.client.name}</b>", styles["normal"]))
        if invoice.client.email:
            elements.append(Paragraph(invoice.client.email, styles["normal"]))
    elements.append(Spacer(1, layout.section_spacer * inch))


def _add_items_table(
    elements: list[Flowable],
    line_items: list[LineItem],
    styles: dict[str, ParagraphStyle],
    layout: _LayoutConfig,
    fonts: _FontConfig,
) -> None:
    """Add the services / line-items table."""
    col_widths = [w * inch for w in layout.items_col_widths]
    header_row = layout.items_header_labels
    data: list[list[object]] = [header_row]  # type: ignore[list-item]

    for desc, qty, unit_price, total in line_items:
        desc_para = Paragraph(str(desc), styles["normal"])
        qty_str = str(int(qty)) if isinstance(qty, int) else f"{qty:.2f}"
        data.append([desc_para, qty_str, f"£{unit_price:.4f}", f"£{total:.2f}"])

    table = Table(data, colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _BLUE),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), fonts.table_header),
                ("FONTSIZE", (0, 1), (-1, -1), fonts.table_body),
                ("TOPPADDING", (0, 0), (-1, -1), 5 if fonts.table_body >= 8 else 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5 if fonts.table_body >= 8 else 4),
                ("LINEBELOW", (0, 0), (-1, 0), 1.5, _BLUE),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, _ZEBRA_GREY],
                ),
            ]
        )
    )
    elements.append(table)


def _add_summary_table(
    elements: list[Flowable],
    invoice: Invoice,
    layout: _LayoutConfig,
    fonts: _FontConfig,
) -> None:
    """Add the financial summary table (subtotal, discount, tax, total, paid, balance)."""
    col_widths = [w * inch for w in layout.summary_col_widths]

    # Label text varies slightly between modes to keep PDFs consistent with
    # the previous hand-written code.
    if fonts.summary_bold >= 12:
        labels = [
            "Subtotal:",
            "Discount:",
            f"Tax ({invoice.tax_rate:.1f}%):",
            "Total Amount:",
            "Amount Paid:",
            "Balance Due:",
        ]
    else:
        labels = [
            "Subtotal:",
            "Discount:",
            f"Tax ({invoice.tax_rate:.1f}%):",
            "Total:",
            "Paid:",
            "Balance Due:",
        ]

    data = [
        [labels[0], f"£{invoice.subtotal:.2f}"],
        [labels[1], f"-£{invoice.discount_amount:.2f}"],
        [labels[2], f"£{invoice.tax_amount:.2f}"],
        [labels[3], f"£{invoice.total_amount:.2f}"],
        [labels[4], f"£{invoice.amount_paid:.2f}"],
        [labels[5], f"£{invoice.balance_due:.2f}"],
    ]

    table = Table(data, colWidths=col_widths)
    style_commands: list[tuple[object, ...]] = [
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTNAME", (0, 5), (-1, 5), "Helvetica-Bold"),
        ("LINEABOVE", (0, 3), (-1, 3), 1.5, colors.black),
    ]

    if fonts.summary_bold >= 12:
        # Download mode sizes
        style_commands += [
            ("FONTSIZE", (0, 0), (-1, -1), fonts.summary_normal),
            ("FONTSIZE", (0, 3), (-1, 3), fonts.summary_bold),
            ("FONTSIZE", (0, 5), (-1, 5), fonts.summary_bold),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
    else:
        # Preview mode sizes
        style_commands += [
            ("FONTSIZE", (0, 0), (-1, 2), fonts.summary_normal),
            ("FONTSIZE", (0, 3), (-1, 3), fonts.summary_bold),
            ("FONTSIZE", (0, 4), (-1, 4), fonts.summary_normal),
            ("FONTSIZE", (0, 5), (-1, 5), fonts.summary_bold),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]

    table.setStyle(TableStyle(style_commands))  # pyright: ignore[reportArgumentType]
    elements.append(table)
