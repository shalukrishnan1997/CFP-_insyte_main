"""Additional invoice editing views for custom line items."""

import json
from decimal import Decimal, InvalidOperation

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from audit.utils import log_request_action
from invoices.models import Invoice, InvoiceSettings
from invoices.utils import get_invoice_line_items
from responsehandling.permissions import is_authenticated_and_is_staff


@is_authenticated_and_is_staff
def invoice_edit(request: HttpRequest, invoice_id: str) -> HttpResponse:
    """Edit invoice with custom line items before generating PDF.

    Allows users to:
    - View all existing metrics
    - Add custom line items
    - Edit quantities and rates
    - Recalculate totals in real-time

    Args:
        request: HTTP request
        invoice_id: UUID of invoice

    Returns:
        Rendered invoice edit page
    """
    invoice = get_object_or_404(
        Invoice.objects.select_related("client", "created_by"), id=invoice_id
    )

    # Get invoice settings for pricing
    settings = InvoiceSettings.get_settings()

    # First check if the user previously selected explicit services (single source of truth)
    # Session key used elsewhere in app: f'invoice_{invoice.id}_services'
    selected_services = request.session.get(f"invoice_{invoice_id}_services", [])

    editable_items = []

    if selected_services:
        # Use user-selected services stored in session (these represent the full service list)
        for service in selected_services:
            qty = service.get("quantity", 1)
            unit_price = Decimal(str(service.get("unit_price", 0)))
            total = unit_price * Decimal(str(qty))
            editable_items.append(
                {
                    "description": service.get("description", ""),
                    "quantity": float(qty) if isinstance(qty, Decimal) else qty,
                    "unit_price": float(unit_price),
                    "total": float(total),
                    "is_custom": False,
                }
            )
    else:
        # Derive default metric-based line items when no explicit service
        # selection has been stored in session.
        line_items = get_invoice_line_items(invoice, settings)

        # Convert to editable format
        for desc, qty, unit_price, total in line_items:
            editable_items.append(
                {
                    "description": desc,
                    "quantity": float(qty) if isinstance(qty, Decimal) else qty,
                    "unit_price": float(unit_price),
                    "total": float(total),
                    "is_custom": False,
                }
            )

    # Get custom line items from session if any
    custom_items = request.session.get(f"invoice_{invoice_id}_custom_items", [])
    for custom_item in custom_items:
        custom_item["is_custom"] = True
    editable_items.extend(custom_items)

    # Calculate financial summary
    subtotal = sum(Decimal(str(item["total"])) for item in editable_items)
    discount = invoice.discount_amount
    subtotal_after_discount = subtotal - discount
    tax_amount = subtotal_after_discount * (invoice.tax_rate / Decimal("100"))
    total_amount = subtotal_after_discount + tax_amount

    context = {
        "invoice": invoice,
        "settings": settings,
        "line_items": editable_items,
        "subtotal": subtotal,
        "tax": tax_amount,
        "total": total_amount,
        "active": "invoices",
        "breadcrumbs": [
            {"name": "Invoices", "url": reverse("custom_admin:invoice_list")},
            {
                "name": invoice.invoice_number,
                "url": reverse(
                    "custom_admin:invoice_detail", kwargs={"invoice_id": invoice_id}
                ),
            },
            {"name": "Edit", "url": None},
        ],
    }

    return render(request, "admin/invoices/edit.html", context)


@is_authenticated_and_is_staff
@require_http_methods(["POST"])
def invoice_update_line_items(request: HttpRequest, invoice_id: str) -> JsonResponse:
    """Update invoice line items via AJAX.

    Accepts JSON with line items array and calculates totals.
    Validates all inputs and recalculates financial summary.

    Args:
        request: HTTP request with JSON body
        invoice_id: UUID of invoice

    Returns:
        JSON response with updated totals or error
    """
    try:
        invoice = get_object_or_404(Invoice, id=invoice_id)

        # Parse JSON data
        data = json.loads(request.body)
        line_items = data.get("line_items", [])

        if not isinstance(line_items, list):
            return JsonResponse({"error": "Invalid line items format"}, status=400)

        # Validate and calculate
        subtotal = Decimal("0")
        validated_items = []

        for item in line_items:
            try:
                desc = str(item.get("description", ""))
                qty = Decimal(str(item.get("quantity", 0)))
                unit_price = Decimal(str(item.get("unit_price", 0)))

                if not desc:
                    return JsonResponse(
                        {"error": "Description cannot be empty"}, status=400
                    )

                if qty < 0 or unit_price < 0:
                    return JsonResponse(
                        {"error": "Quantity and price must be positive"}, status=400
                    )

                total = qty * unit_price
                subtotal += total

                validated_items.append(
                    {
                        "description": desc,
                        "quantity": float(qty),
                        "unit_price": float(unit_price),
                        "total": float(total),
                        "is_custom": item.get("is_custom", False),
                    }
                )

            except (ValueError, InvalidOperation, TypeError) as e:
                return JsonResponse(
                    {"error": f"Invalid number format: {e!s}"}, status=400
                )

        # Calculate financial totals
        discount = invoice.discount_amount
        tax_rate = invoice.tax_rate

        subtotal_after_discount = subtotal - discount
        tax_amount = subtotal_after_discount * (tax_rate / Decimal("100"))
        total_amount = subtotal_after_discount + tax_amount
        balance_due = total_amount - invoice.amount_paid

        # Store custom items in session
        custom_items = [item for item in validated_items if item.get("is_custom")]
        request.session[f"invoice_{invoice_id}_custom_items"] = custom_items

        # Audit log for invoice edit
        log_request_action(
            request,
            action="UPDATE",
            model_name="Invoice",
            object_id=str(invoice_id),
            object_repr=f"Invoice {invoice.invoice_number} line items",
            changes={"custom_line_items": {"new": custom_items}},
            summary=f"Updated line items for invoice {invoice.invoice_number} ({len(custom_items)} custom items)",
        )

        return JsonResponse(
            {
                "success": True,
                "line_items": validated_items,
                "financial_summary": {
                    "subtotal": float(subtotal),
                    "discount": float(discount),
                    "tax_rate": float(tax_rate),
                    "tax_amount": float(tax_amount),
                    "total_amount": float(total_amount),
                    "amount_paid": float(invoice.amount_paid),
                    "balance_due": float(balance_due),
                },
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
