from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from invoices.models import InvoiceSettings
from responsehandling.permissions import (
    is_authenticated_and_is_staff,
)


@is_authenticated_and_is_staff
def admin_dashboard(request: HttpRequest) -> HttpResponse:
    """Display the main admin dashboard.

    Shows overview statistics and quick access to main system features.
    Requires authenticated staff user.

    Args:
        request: The HTTP request object.

    Returns:
        HttpResponse: Rendered dashboard template.
    """
    context = {
        "active": "dashboard",
    }
    return render(request, "admin/dashboard.html", context)


@is_authenticated_and_is_staff
def admin_settings(request: HttpRequest) -> HttpResponse:
    """System settings and configuration.

    Manages Invoice Settings including default pricing per metric,
    company details, and general invoice configuration.

    Args:
        request: HTTP request with optional POST data for settings updates.

    Returns:
        Rendered settings page.
    """
    settings = InvoiceSettings.get_settings()

    if request.method == "POST":
        # Get active tab for redirect
        request.POST.get("active_tab", "invoice")

        # Helper to safely convert to Decimal
        def safe_decimal(value: str, default: Decimal = Decimal("0")) -> Decimal:
            try:
                return Decimal(value) if value else default
            except InvalidOperation:
                return default

        # Update pricing fields
        settings.price_per_donation = safe_decimal(
            request.POST.get("price_per_donation", "0")
        )
        settings.price_per_gift_aid = safe_decimal(
            request.POST.get("price_per_gift_aid", "0")
        )
        settings.price_per_letter = safe_decimal(
            request.POST.get("price_per_letter", "0")
        )
        settings.price_per_campaign = safe_decimal(
            request.POST.get("price_per_campaign", "0")
        )
        settings.price_per_hgv = safe_decimal(request.POST.get("price_per_hgv", "0"))
        settings.price_per_lgv = safe_decimal(request.POST.get("price_per_lgv", "0"))
        settings.price_per_donor_response = safe_decimal(
            request.POST.get("price_per_donor_response", "0")
        )
        settings.price_per_notification = safe_decimal(
            request.POST.get("price_per_notification", "0")
        )
        settings.price_per_data_file = safe_decimal(
            request.POST.get("price_per_data_file", "0")
        )
        settings.price_per_batch = safe_decimal(
            request.POST.get("price_per_batch", "0")
        )
        settings.price_per_report = safe_decimal(
            request.POST.get("price_per_report", "0")
        )
        settings.price_per_template = safe_decimal(
            request.POST.get("price_per_template", "0")
        )
        settings.price_per_user = safe_decimal(request.POST.get("price_per_user", "0"))
        settings.price_per_donor_added = safe_decimal(
            request.POST.get("price_per_donor_added", "0")
        )
        settings.price_per_donor_updated = safe_decimal(
            request.POST.get("price_per_donor_updated", "0")
        )
        settings.price_per_api_call = safe_decimal(
            request.POST.get("price_per_api_call", "0")
        )
        settings.price_per_mb_storage = safe_decimal(
            request.POST.get("price_per_mb_storage", "0")
        )

        # Update default settings
        settings.default_tax_rate = safe_decimal(
            request.POST.get("default_tax_rate", "20")
        )
        due_days_str = request.POST.get("default_due_days", "30")
        try:
            settings.default_due_days = int(due_days_str) if due_days_str else 30
        except ValueError:
            settings.default_due_days = 30

        # Update company details
        settings.company_name = request.POST.get("company_name", "").strip()
        settings.company_address = request.POST.get("company_address", "").strip()
        settings.company_phone = request.POST.get("company_phone", "").strip()
        settings.company_email = request.POST.get("company_email", "").strip()
        settings.company_vat_number = request.POST.get("company_vat_number", "").strip()
        settings.company_logo_url = request.POST.get("company_logo_url", "").strip()

        # Track who updated
        settings.updated_by = request.user
        settings.save()

        messages.success(request, "Settings updated successfully!")
        return redirect("custom_admin:admin_settings")

    context = {
        "active": "settings",
        "settings": settings,
        "active_tab": request.GET.get("tab", "invoice"),
    }
    return render(request, "admin/settings.html", context)
