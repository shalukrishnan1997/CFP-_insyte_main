"""Views for managing service categories and items in invoice settings."""

import json
from decimal import Decimal

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from invoices.models import ServiceCategory, ServiceItem
from responsehandling.permissions import is_authenticated_and_is_staff


@is_authenticated_and_is_staff
def service_settings(request: HttpRequest) -> HttpResponse:
    """Display and manage service categories and items for invoice line item selection.

    Professional elite UI with categorized display matching invoice settings design.
    Allows admin to:
    - View all service categories and items hierarchically
    - Edit pricing and descriptions inline
    - Toggle active/inactive status
    - Mark items as default for invoice
    - Add new services and categories
    """
    # Get all categories with their service items, optimized query
    categories = ServiceCategory.objects.prefetch_related("service_items").order_by(
        "order", "name"
    )

    # Calculate statistics
    total_services = ServiceItem.objects.count()
    active_services = ServiceItem.objects.filter(is_active=True).count()
    default_services = ServiceItem.objects.filter(is_default=True).count()

    context = {
        "active": "settings",
        "categories": categories,
        "total_services": total_services,
        "active_services": active_services,
        "default_services": default_services,
        "breadcrumbs": [
            {"name": "Settings", "url": reverse("custom_admin:admin_settings")},
            {"name": "Service Line Items", "url": None},
        ],
    }

    return render(request, "admin/settings/service_settings_create.html", context)


def _parse_request_data(request: HttpRequest) -> dict:
    """Extract data from JSON body or POST form data."""
    if request.content_type == "application/json":
        return json.loads(request.body)
    return request.POST


def _parse_bool_value(value: str | bool) -> bool:
    """Parse a string/bool into a boolean value."""
    return str(value).lower() in ("true", "1", "yes")


def _apply_field_update(item: ServiceItem, field: str, value: object) -> bool:
    """Apply a single field update to a ServiceItem, return True on success."""
    if field == "unit_price":
        item.unit_price = Decimal(str(value))
    elif field in ("pricing_unit", "description", "notes"):
        setattr(item, field, value)
    elif field in ("is_active", "is_default"):
        if value == "toggle":
            setattr(item, field, not getattr(item, field))
        else:
            setattr(item, field, _parse_bool_value(value))  # pyright: ignore[reportArgumentType]
    else:
        return False
    return True


@is_authenticated_and_is_staff
@require_http_methods(["POST"])
def service_item_update(request: HttpRequest) -> JsonResponse:
    """Update service item via AJAX. Accepts both JSON and form data."""
    try:
        data = _parse_request_data(request)
        item_id = data.get("service_id") or data.get("item_id")
        field = data.get("field")
        value = data.get("value")

        item = get_object_or_404(ServiceItem, id=item_id)

        if not _apply_field_update(item, field, value):  # pyright: ignore[reportArgumentType]
            return JsonResponse(
                {"success": False, "error": "Invalid field"}, status=400
            )

        item.save()

        return JsonResponse(
            {
                "success": True,
                "message": f"Updated {item.description}",
                "is_active": item.is_active,
                "is_default": item.is_default,
                "item": {
                    "id": str(item.id),
                    "description": item.description,
                    "unit_price": str(item.unit_price),
                    "pricing_unit": item.pricing_unit,
                    "notes": item.notes,
                    "is_active": item.is_active,
                    "is_default": item.is_default,
                },
            }
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@is_authenticated_and_is_staff
@require_http_methods(["POST"])
def service_category_update(request: HttpRequest) -> JsonResponse:
    """Update service category via AJAX. Accepts both JSON and form data."""
    try:
        data = _parse_request_data(request)
        category_id = data.get("category_id")
        field = data.get("field")
        value = data.get("value")

        category = get_object_or_404(ServiceCategory, id=category_id)

        if field == "name":
            category.name = value
        elif field == "description":
            category.description = value
        elif field == "is_active":
            category.is_active = str(value or "").lower() == "true"
        elif field == "order":
            category.order = int(value or 0)  # pyright: ignore[reportArgumentType]
        else:
            return JsonResponse(
                {"success": False, "error": "Invalid field"}, status=400
            )

        category.save()

        return JsonResponse(
            {
                "success": True,
                "message": f"Updated category: {category.name}",
                "category": {
                    "id": str(category.id),
                    "name": category.name,
                    "description": category.description,
                    "is_active": category.is_active,
                    "order": category.order,
                },
            }
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@is_authenticated_and_is_staff
def service_item_create(request: HttpRequest) -> HttpResponse:
    """Create new service item. Accepts both JSON and form data."""
    if request.method == "POST":
        try:
            data = _parse_request_data(request)
            category_id = data.get("category_id")
            description = data.get("description")
            unit_price = Decimal(data.get("unit_price", "0.00"))
            pricing_unit = data.get("pricing_unit", "each")
            notes = data.get("notes", "")
            is_default = (
                data.get("is_default", False)
                if request.content_type == "application/json"
                else data.get("is_default") == "on"
            )

            category = get_object_or_404(ServiceCategory, id=category_id)

            # Get max order for this category
            max_order = ServiceItem.objects.filter(category=category).count()

            item = ServiceItem.objects.create(
                category=category,
                description=description,
                unit_price=unit_price,
                pricing_unit=pricing_unit,
                notes=notes,
                is_default=is_default,
                is_active=True,
                order=max_order + 1,
            )

            # Return JSON for AJAX requests
            if request.content_type == "application/json":
                return JsonResponse(
                    {
                        "success": True,
                        "message": f"Created service item: {item.description}",
                        "item": {
                            "id": str(item.id),
                            "description": item.description,
                            "unit_price": str(item.unit_price),
                            "pricing_unit": item.pricing_unit,
                        },
                    }
                )

            messages.success(request, f"Created service item: {item.description}")
            return redirect("custom_admin:admin_service_settings")
        except Exception as e:
            if request.content_type == "application/json":
                return JsonResponse({"success": False, "error": str(e)}, status=500)
            messages.error(request, f"Error creating service item: {e!s}")
            return redirect("custom_admin:admin_service_settings")

    # Legacy GET form path no longer exists; keep a safe redirect.
    return redirect("custom_admin:admin_service_settings")


@is_authenticated_and_is_staff
def service_item_delete(request: HttpRequest, item_id: str) -> HttpResponse:
    """Soft delete service item (mark as inactive)."""
    item = get_object_or_404(ServiceItem, id=item_id)
    item.is_active = False
    item.save()
    messages.success(request, f"Deactivated service item: {item.description}")
    return redirect("custom_admin:admin_service_settings")


@is_authenticated_and_is_staff
def service_category_create(request: HttpRequest) -> HttpResponse:
    """Create new service category. Accepts both JSON and form data."""
    if request.method == "POST":
        try:
            data = _parse_request_data(request)
            name = data.get("name")
            description = data.get("description", "")

            # Get max order
            max_order = ServiceCategory.objects.count()

            category = ServiceCategory.objects.create(
                name=name, description=description, is_active=True, order=max_order + 1
            )

            # Return JSON for AJAX requests
            if request.content_type == "application/json":
                return JsonResponse(
                    {
                        "success": True,
                        "message": f"Created category: {category.name}",
                        "category": {
                            "id": str(category.id),
                            "name": category.name,
                            "description": category.description,
                        },
                    }
                )

            messages.success(request, f"Created category: {category.name}")
            return redirect("custom_admin:admin_service_settings")
        except Exception as e:
            if request.content_type == "application/json":
                return JsonResponse({"success": False, "error": str(e)}, status=500)
            messages.error(request, f"Error creating category: {e!s}")
            return redirect("custom_admin:admin_service_settings")

    # Legacy GET form path no longer exists; keep a safe redirect.
    return redirect("custom_admin:admin_service_settings")


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def service_items_api(request: HttpRequest) -> JsonResponse:
    """API endpoint to fetch service items for invoice line item selection.

    Returns JSON structure:
    {
        "categories": [
            {
                "id": "uuid",
                "name": "Set Ups",
                "items": [
                    {
                        "id": "uuid",
                        "description": "Payment Gateway integration",
                        "unit_price": "400.00",
                        "pricing_unit": "each",
                        "notes": "ie: SagePay, Barclay Card, WorldPay..."
                    }
                ]
            }
        ]
    }
    """
    categories = ServiceCategory.objects.filter(is_active=True).prefetch_related(
        "service_items"
    )

    data = {
        "categories": [
            {
                "id": str(cat.id),
                "name": cat.name,
                "description": cat.description,
                "service_items": [
                    {
                        "id": str(item.id),
                        "description": item.description,
                        "unit_price": str(item.unit_price),
                        "pricing_unit": item.pricing_unit,
                        "notes": item.notes,
                        "is_default": item.is_default,
                        "is_active": item.is_active,
                    }
                    for item in cat.service_items.filter(is_active=True).order_by(
                        "order"
                    )
                ],
            }
            for cat in categories
        ]
    }

    return JsonResponse(data)
