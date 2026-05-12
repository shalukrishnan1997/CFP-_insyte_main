"""Invoice views for managing and generating client invoices.

Provides CRUD views for invoice management, PDF generation/preview (via
:class:`~core.services.invoice_pdf.InvoicePdfService`), and a
metrics API endpoint (via
:class:`~core.services.invoice_metrics.InvoiceMetricsService`).
"""

import contextlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from audit.utils import log_request_action
from campaigns.models import Campaign
from clients.models import Client
from core.models import User
from core.pagination import paginate_queryset
from donations.models import Donation
from invoices.forms import InvoiceSendEmailForm
from invoices.metrics import InvoiceMetricsService
from invoices.models import Invoice, InvoiceSettings
from invoices.pdf import InvoicePdfService, PdfMode
from invoices.services import InvoiceService
from invoices.utils import (
    calculate_all_line_totals,
    get_invoice_line_items,
)
from responsehandling.permissions import is_authenticated_and_is_staff

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_invoice_selected_services(
    invoice: Invoice, request: HttpRequest
) -> list[tuple[str, int, Decimal, Decimal]]:
    """Get selected services from session for an invoice.

    This is the SINGLE SOURCE OF TRUTH for selected services.

    Args:
        invoice: Invoice object.
        request: HTTP request (to access session).

    Returns:
        List of ``(description, quantity, unit_price, line_total)`` tuples.
    """
    line_items: list[tuple[str, int, Decimal, Decimal]] = []
    selected_services = get_invoice_session_items(request, invoice.id, "services")

    for service in selected_services:
        qty = int(service.get("quantity", 1))  # type: ignore[arg-type]
        unit_price = Decimal(str(service.get("unit_price", 0)))
        line_total = unit_price * qty
        line_items.append(
            (str(service.get("description", "Service")), qty, unit_price, line_total)
        )

    return line_items


def _collect_line_items(
    invoice: Invoice,
    request: HttpRequest,
    settings: InvoiceSettings,
) -> list[tuple[str, int | float, Decimal, Decimal]]:
    """Collect all line items for an invoice (services + custom items).

    Args:
        invoice: Invoice instance.
        request: HTTP request (for session access).
        settings: Invoice settings used for default metric pricing.

    Returns:
        Combined list of line-item tuples.
    """
    line_items = get_invoice_selected_services(invoice, request)
    if not line_items:
        line_items = get_invoice_line_items(invoice, settings)

    # Append custom items from session
    custom_items = get_invoice_session_items(request, invoice.id, "custom_items")
    for item in custom_items:
        line_items.append(
            (
                str(item["description"]),
                int(item["quantity"]),  # type: ignore[arg-type]
                Decimal(str(item["unit_price"])),
                Decimal(str(item["total"])),
            )
        )
    return line_items  # pyright: ignore[reportReturnType]


# ---------------------------------------------------------------------------
# List / Detail views
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_list(request: HttpRequest) -> HttpResponse:
    """Display list of all invoices with filtering.

    Args:
        request: HTTP request.

    Returns:
        Rendered invoice list page.
    """
    client_filter = request.GET.get("client", "")
    status_filter = request.GET.get("status", "")
    search_query = request.GET.get("search", "").strip()
    per_page = int(request.GET.get("per_page", 20))

    invoices_qs = Invoice.objects.select_related("client", "created_by").order_by(
        "-issue_date", "-invoice_number"
    )

    if client_filter:
        invoices_qs = invoices_qs.filter(client_id=client_filter)
    if status_filter:
        invoices_qs = invoices_qs.filter(status=status_filter)
    if search_query:
        invoices_qs = invoices_qs.filter(
            Q(invoice_number__icontains=search_query)
            | Q(client__name__icontains=search_query)
        )

    invoices = paginate_queryset(
        invoices_qs,
        request,
        per_page=per_page,
        per_page_param="per_page",
    )

    all_clients = Client.objects.filter(is_active=True).order_by("name")

    total_invoices = Invoice.objects.count()
    total_outstanding = Invoice.objects.filter(
        status__in=[Invoice.STATUS_ISSUED, Invoice.STATUS_OVERDUE]
    ).aggregate(total=Sum("balance_due"))["total"] or Decimal("0")

    context = {
        "invoices": invoices,
        "all_clients": all_clients,
        "status_choices": Invoice.STATUS_CHOICES,
        "client_filter": client_filter,
        "status_filter": status_filter,
        "search_query": search_query,
        "per_page": per_page,
        "total_invoices": total_invoices,
        "total_outstanding": total_outstanding,
        "active": "invoices",
        "breadcrumbs": [{"name": "Invoices", "url": None}],
    }

    return render(request, "admin/invoices/list.html", context)


@is_authenticated_and_is_staff
def invoice_detail(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Display detailed invoice view with all selected services.

    Args:
        request: HTTP request.
        invoice_id: UUID of invoice.

    Returns:
        Rendered invoice detail page.
    """
    invoice = get_invoice_with_relations(invoice_id)
    inv_settings = InvoiceSettings.get_settings()
    selected_services = get_invoice_selected_services(invoice, request)

    log_request_action(
        request,
        action="VIEW",
        model_name="Invoice",
        object_id=str(invoice.id),
        object_repr=invoice.invoice_number,
        summary=f"Viewed invoice {invoice.invoice_number} for {invoice.client.name} with {len(selected_services)} service line items",
    )

    return render(
        request,
        "admin/invoices/detail.html",
        build_invoice_detail_context(invoice, selected_services, inv_settings),
    )


# ---------------------------------------------------------------------------
# Status / Payment views
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_mark_paid(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Mark an invoice as paid.

    Args:
        request: HTTP request.
        invoice_id: UUID of invoice.

    Returns:
        Redirect to invoice detail.
    """
    if request.method != "POST":
        return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)

    invoice = get_object_or_404(Invoice, id=invoice_id)

    amount = Decimal(request.POST.get("amount") or str(invoice.total_amount))
    payment_method = request.POST.get("payment_method", "")
    payment_reference = request.POST.get("payment_reference", "")
    payment_date = request.POST.get("payment_date", timezone.now().date())

    old_status = invoice.status
    old_amount_paid = invoice.amount_paid

    invoice.amount_paid = amount
    invoice.payment_method = payment_method
    invoice.payment_reference = payment_reference
    invoice.payment_date = payment_date
    invoice.save()

    log_request_action(
        request,
        action="UPDATE",
        model_name="Invoice",
        object_id=str(invoice.id),
        object_repr=f"Invoice {invoice.invoice_number}",
        changes={
            "status": {"old": old_status, "new": invoice.status},
            "amount_paid": {"old": float(old_amount_paid), "new": float(amount)},
            "payment_method": {"old": "", "new": payment_method},
            "payment_reference": {"old": "", "new": payment_reference},
        },
        summary=f"Marked invoice {invoice.invoice_number} as paid (£{amount})",
    )

    messages.success(request, f"Invoice {invoice.invoice_number} marked as paid.")
    return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)


@is_authenticated_and_is_staff
def invoice_change_status(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Change invoice status.

    Allows changing to: draft, issued, overdue, cancelled.
    For 'paid', use :func:`invoice_mark_paid` instead.

    Args:
        request: HTTP request.
        invoice_id: UUID of invoice.

    Returns:
        Redirect to invoice detail.
    """
    if request.method != "POST":
        return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)

    invoice = get_object_or_404(Invoice, id=invoice_id)
    new_status = request.POST.get("status", "")

    valid_statuses = [
        Invoice.STATUS_DRAFT,
        Invoice.STATUS_ISSUED,
        Invoice.STATUS_OVERDUE,
        Invoice.STATUS_CANCELLED,
    ]
    if new_status not in valid_statuses:
        messages.error(request, f"Invalid status: {new_status}")
        return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)

    old_status = invoice.status
    invoice.status = new_status
    invoice.save(update_fields=["status", "updated_at"])

    log_request_action(
        request,
        action="UPDATE",
        model_name="Invoice",
        object_id=str(invoice.id),
        object_repr=f"Invoice {invoice.invoice_number}",
        changes={"status": {"old": old_status, "new": new_status}},
        summary=f"Changed invoice {invoice.invoice_number} status from {old_status} to {new_status}",
    )

    messages.success(
        request, f"Invoice {invoice.invoice_number} status changed to {new_status}."
    )
    return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_send_email_view(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Render and process the 'Send by email' form for an invoice.

    GET renders the form pre-filled with the client's primary contact email.
    POST validates the form, dispatches the email through
    :meth:`InvoiceService.send_invoice_by_email`, and redirects back to the
    invoice detail page with a success or error message.

    Args:
        request: HTTP request.
        invoice_id: UUID of the invoice to email.

    Returns:
        Rendered form on GET / invalid POST, redirect to detail on success.
    """
    invoice = get_invoice_with_relations(invoice_id)

    if request.method == "POST":
        form = InvoiceSendEmailForm(request.POST)
        if form.is_valid():
            try:
                # cast: pyright sees request.user as AbstractBaseUser/Anon
                assert isinstance(request.user, User)
                InvoiceService.send_invoice_by_email(
                    invoice,
                    to=form.cleaned_data["to"],
                    cc=form.cleaned_data["cc"],
                    body=form.cleaned_data["message"] or None,
                    sender=request.user,
                )
            except Exception as exc:
                messages.error(request, f"Failed to send invoice email: {exc!s}")
            else:
                recipients = ", ".join(form.cleaned_data["to"])
                messages.success(
                    request,
                    f"Invoice {invoice.invoice_number} emailed to {recipients}.",
                )
                return redirect("custom_admin:invoice_detail", invoice_id=invoice_id)
    else:
        form = InvoiceSendEmailForm(initial={"to": invoice.client.email or ""})

    context = {
        "invoice": invoice,
        "form": form,
        "active": "invoices",
        "breadcrumbs": [
            {"name": "Invoices", "url": reverse("custom_admin:invoice_list")},
            {
                "name": invoice.invoice_number,
                "url": reverse("custom_admin:invoice_detail", args=[str(invoice.id)]),
            },
            {"name": "Send by email", "url": None},
        ],
    }
    return render(request, "admin/invoices/send_email.html", context)


# ---------------------------------------------------------------------------
# PDF generation / preview
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_generate_pdf(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Generate and download a PDF invoice.

    Uses :class:`~core.services.invoice_pdf.InvoicePdfService` for
    ReportLab-based PDF rendering. Shows an HTML invoice view on error.

    Args:
        request: HTTP request.
        invoice_id: UUID of invoice.

    Returns:
        PDF file download response or HTML invoice response on error.
    """
    return _pdf_response(request, invoice_id, mode="download")


@is_authenticated_and_is_staff
def invoice_preview_pdf(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Preview PDF inline in the browser at A4 dimensions.

    Uses :class:`~core.services.invoice_pdf.InvoicePdfService` for
    ReportLab-based PDF rendering. Shows an HTML invoice view on error.

    Args:
        request: HTTP request.
        invoice_id: UUID of invoice.

    Returns:
        PDF inline response or HTML invoice response on error.
    """
    return _pdf_response(request, invoice_id, mode="preview")


def _pdf_response(
    request: HttpRequest, invoice_id: str, *, mode: PdfMode
) -> HttpResponse:
    """Shared implementation for PDF download and preview.

    Args:
        request: HTTP request.
        invoice_id: Invoice UUID.
        mode: ``"download"`` or ``"preview"``.

    Returns:
        ``HttpResponse`` with PDF content or an HTML invoice response on error.
    """
    invoice = get_invoice_with_relations(invoice_id)
    inv_settings = InvoiceSettings.get_settings()
    line_totals = calculate_all_line_totals(invoice, inv_settings)
    line_items = _collect_line_items(invoice, request, inv_settings)

    try:
        pdf_bytes = InvoicePdfService.build_pdf(
            invoice, line_items, inv_settings, mode=mode
        )

        return build_invoice_pdf_success_response(request, invoice, pdf_bytes, mode)

    except Exception as exc:
        return build_invoice_pdf_fallback_response(
            request,
            invoice,
            inv_settings,
            line_totals,
            mode,
            exc,
        )


# ---------------------------------------------------------------------------
# Metrics API
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_metrics_api(request: HttpRequest) -> JsonResponse:
    """API endpoint to calculate invoice metrics for a billing period.

    Query params: ``client_id``, ``start_date``, ``end_date``,
    optional ``campaign_id``.

    Args:
        request: HTTP request.

    Returns:
        JSON response with calculated metrics.
    """
    client_id = request.GET.get("client_id")
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")
    campaign_id = request.GET.get("campaign_id")

    if not all([client_id, start_date, end_date]):
        return JsonResponse({"error": "Missing required parameters"}, status=400)

    try:
        client = Client.objects.get(id=client_id)
        campaign = Campaign.objects.get(id=campaign_id) if campaign_id else None

        if campaign and campaign.client_id != client.id:
            return JsonResponse(
                {
                    "error": (
                        "Selected campaign does not belong to the selected client."
                    )
                },
                status=400,
            )

        assert start_date is not None  # guaranteed by all() check above
        assert end_date is not None  # guaranteed by all() check above
        metrics = InvoiceMetricsService.calculate(
            client, start_date, end_date, campaign
        )

        return JsonResponse(serialize_invoice_metrics(metrics))
    except Client.DoesNotExist, Campaign.DoesNotExist:
        return JsonResponse({"error": "Client or campaign not found"}, status=404)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Invoice creation
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def invoice_create_with_services(request: HttpRequest) -> HttpResponse:
    """Enhanced invoice creation with service selection UI.

    Uses :class:`~core.services.invoice_metrics.InvoiceMetricsService`
    for automated metrics calculation.

    Args:
        request: HTTP request.

    Returns:
        Rendered creation page or redirect to detail on success.
    """
    if request.method == "POST":
        return _handle_invoice_create_post(request)

    # GET — show creation form
    clients = Client.objects.filter(is_active=True).order_by("name")
    if not clients.exists():
        messages.warning(
            request, "No active clients found. Please create a client first."
        )
        return redirect("custom_admin:client_setup")

    campaigns = (
        Campaign.objects.select_related("client")
        .filter(client__is_active=True)
        .order_by("client__name", "name")
    )
    inv_settings = InvoiceSettings.get_settings()

    context = {
        "clients": clients,
        "campaigns": campaigns,
        "settings": inv_settings,
        "active": "invoices",
        "breadcrumbs": [
            {"name": "Invoices", "url": reverse("custom_admin:invoice_list")},
            {"name": "Create Invoice", "url": None},
        ],
    }
    return render(request, "admin/invoices/create_elite.html", context)


def _handle_invoice_create_post(request: HttpRequest) -> HttpResponse:
    """Process POST for invoice creation with services.

    Args:
        request: HTTP request.

    Returns:
        Redirect to invoice detail on success, or back to create form.
    """
    payload = parse_invoice_create_payload(request)
    client = get_object_or_404(Client, id=payload.client_id)

    if not payload.campaign_id:
        messages.error(
            request,
            "Campaign is required. Please select a campaign for this invoice.",
        )
        return redirect("custom_admin:invoice_create")

    campaign = get_object_or_404(Campaign, id=payload.campaign_id, client=client)

    invoice = create_invoice_record(
        user=request.user,
        client=client,
        campaign=campaign,
        payload=payload,
    )

    link_invoice_donations(
        invoice,
        client,
        campaign,
        payload.billing_period_start,
        payload.billing_period_end,
    )
    finalize_invoice_create(request, invoice, payload.selected_services)
    return redirect("custom_admin:invoice_detail", invoice_id=invoice.id)


# ---------------------------------------------------------------------------
# Invoice helpers (inlined from invoice_helpers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceCreatePayload:
    """Normalized POST payload for invoice creation."""

    client_id: str | None
    campaign_id: str | None
    billing_period_start: str
    billing_period_end: str
    due_days: int
    tax_rate: Decimal
    notes: str
    terms: str
    selected_services: list[dict[str, object]]


def build_invoice_session_key(invoice_id: object, suffix: str) -> str:
    """Return the stable session key used for invoice edit state."""
    return f"invoice_{invoice_id}_{suffix}"


def get_invoice_session_items(
    request: HttpRequest,
    invoice_id: object,
    suffix: str,
) -> list[dict[str, object]]:
    """Return a list of session-backed invoice items."""
    session_items = request.session.get(
        build_invoice_session_key(invoice_id, suffix), []
    )
    return session_items if isinstance(session_items, list) else []


def get_invoice_with_relations(invoice_id: str) -> Invoice:
    """Fetch an invoice with the related objects used across invoice views."""
    return get_object_or_404(
        Invoice.objects.select_related("client", "created_by"), id=invoice_id
    )


def serialize_invoice_metrics(metrics: dict[str, object]) -> dict[str, object]:
    """Map service metrics into the JSON contract returned by the API."""
    return {
        "donations": metrics.get("total_donations_captured", 0),
        "gift_aid": metrics.get("gift_aid_captured", 0),
        "letters": metrics.get("letters_generated", 0),
        "campaigns": metrics.get("campaigns_created", 0),
        "hgv": metrics.get("hgv_identified", 0),
        "lgv": metrics.get("lgv_identified", 0),
    }


def build_invoice_detail_context(
    invoice: Invoice,
    selected_services: list[tuple[str, int, Decimal, Decimal]],
    inv_settings: InvoiceSettings,
) -> dict[str, object]:
    """Build template context for the invoice detail page."""
    return {
        "invoice": invoice,
        "settings": inv_settings,
        "selected_services": selected_services,
        "has_selected_services": len(selected_services) > 0,
        "active": "invoices",
        "breadcrumbs": [
            {"name": "Invoices", "url": reverse("custom_admin:invoice_list")},
            {"name": invoice.invoice_number, "url": None},
        ],
        **calculate_all_line_totals(invoice, inv_settings),
    }


def build_invoice_pdf_success_response(
    request: HttpRequest,
    invoice: Invoice,
    pdf_bytes: bytes,
    mode: PdfMode,
) -> HttpResponse:
    """Build the successful PDF response and write the audit record."""
    audit_action = "DOWNLOAD" if mode == "download" else "VIEW"
    audit_suffix = "PDF" if mode == "download" else "PDF Preview"
    log_request_action(
        request,
        action=audit_action,
        model_name="Invoice",
        object_id=str(invoice.id),
        object_repr=f"Invoice {invoice.invoice_number} {audit_suffix}",
        summary=(
            f"{'Generated and downloaded' if mode == 'download' else 'Previewed'} "
            f"PDF for invoice {invoice.invoice_number}"
            + (f" ({invoice.client.name})" if mode == "download" else "")
        ),
    )

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = InvoicePdfService.get_content_disposition(
        invoice, mode=mode
    )
    return response


def build_invoice_pdf_fallback_response(
    request: HttpRequest,
    invoice: Invoice,
    inv_settings: InvoiceSettings,
    line_totals: dict[str, Decimal],
    mode: PdfMode,
    exc: Exception,
) -> HttpResponse:
    """Return the HTML fallback response used when PDF generation fails."""
    html_string = render_to_string(
        "admin/invoices/invoice_pdf.html",
        {"invoice": invoice, "settings": inv_settings, **line_totals},
    )
    level = messages.warning if mode == "download" else messages.info
    level(
        request,
        f"PDF {'generation' if mode == 'download' else 'preview'} error: "
        f"{exc!s}. Showing HTML version"
        + (" - use browser's Print to PDF." if mode == "download" else "."),
    )
    return HttpResponse(html_string, content_type="text/html")


def calculate_selected_service_fee(
    selected_services: list[dict[str, Any]],
) -> Decimal:
    """Return the total fee represented by the selected service list."""
    return sum(
        (
            Decimal(str(service["unit_price"])) * int(str(service["quantity"]))
            for service in selected_services
        ),
        Decimal("0.00"),
    )


def parse_invoice_create_payload(request: HttpRequest) -> InvoiceCreatePayload:
    """Normalize invoice-create POST values into a typed payload."""
    return InvoiceCreatePayload(
        client_id=request.POST.get("client_id"),
        campaign_id=request.POST.get("campaign_id") or None,
        billing_period_start=request.POST.get("billing_period_start", ""),
        billing_period_end=request.POST.get("billing_period_end", ""),
        due_days=int(request.POST.get("due_days", 30)),
        tax_rate=Decimal(request.POST.get("tax_rate", "0")),
        notes=request.POST.get("notes", ""),
        terms=request.POST.get("terms_and_conditions", ""),
        selected_services=json.loads(request.POST.get("selected_services", "[]")),
    )


def create_invoice_record(
    *,
    user: object,
    client: Client,
    campaign: Campaign,
    payload: InvoiceCreatePayload,
) -> Invoice:
    """Create the invoice row for the submitted billing period."""
    metrics = InvoiceMetricsService.calculate(
        client,
        payload.billing_period_start,
        payload.billing_period_end,
        campaign=campaign,
    )
    service_fee = calculate_selected_service_fee(payload.selected_services)

    return Invoice.objects.create(
        client=client,
        campaign=campaign,
        billing_period_start=payload.billing_period_start,
        billing_period_end=payload.billing_period_end,
        issue_date=timezone.now().date(),
        due_date=timezone.now().date() + timedelta(days=payload.due_days),
        created_by=user,
        status=Invoice.STATUS_DRAFT,
        notes=payload.notes,
        terms_and_conditions=payload.terms,
        **metrics,
        service_fee=service_fee,
        processing_fee=Decimal("0"),
        setup_fee=Decimal("0"),
        additional_charges=Decimal("0"),
        discount_amount=Decimal("0"),
        tax_rate=payload.tax_rate,
    )


def _parse_billing_period_dates(
    billing_period_start: str,
    billing_period_end: str,
) -> tuple[date | None, date | None]:
    """Parse billing period bounds in ISO or UK date formats."""
    start = parse_date(billing_period_start)
    end = parse_date(billing_period_end)

    if start is None:
        with contextlib.suppress(ValueError, TypeError):
            start = datetime.strptime(billing_period_start, "%d/%m/%Y").date()
    if end is None:
        with contextlib.suppress(ValueError, TypeError):
            end = datetime.strptime(billing_period_end, "%d/%m/%Y").date()

    return start, end


def link_invoice_donations(
    invoice: Invoice,
    client: Client,
    campaign: Campaign,
    billing_period_start: str,
    billing_period_end: str,
) -> None:
    """Attach billing-period donations to the created invoice."""
    start, end = _parse_billing_period_dates(billing_period_start, billing_period_end)
    donation_filters: dict[str, object] = {"campaign": campaign}

    if start is not None:
        donation_filters["created_at__date__gte"] = start
    if end is not None:
        donation_filters["created_at__date__lte"] = end

    donations_qs = Donation.objects.filter(**donation_filters)
    if start is None and end is None:
        donations_qs = Donation.objects.filter(
            campaign__client=client, campaign=campaign
        )

    invoice.donations.set(donations_qs)


def store_invoice_services(
    request: HttpRequest,
    invoice_id: object,
    selected_services: list[dict[str, object]],
) -> None:
    """Persist the selected services in the session for later invoice views."""
    request.session[build_invoice_session_key(invoice_id, "services")] = (
        selected_services
    )


def finalize_invoice_create(
    request: HttpRequest,
    invoice: Invoice,
    selected_services: list[dict[str, object]],
) -> None:
    """Persist create-time state, audit the action, and queue the success message."""
    store_invoice_services(request, invoice.id, selected_services)
    log_request_action(
        request,
        action="CREATE",
        model_name="Invoice",
        object_id=str(invoice.id),
        object_repr=f"Invoice {invoice.invoice_number} for {invoice.client.name}",
        summary=f"Created invoice {invoice.invoice_number} with {len(selected_services)} service line items (\u00a3{invoice.total_amount})",
    )
    messages.success(
        request,
        f"Invoice {invoice.invoice_number} created successfully "
        f"with {len(selected_services)} services.",
    )
