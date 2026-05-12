"""API endpoint for quick donation editing from QA batch review."""

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from donations.models import Donation
from responsehandling.permissions import has_permission_or_is_staff


@require_http_methods(["POST"])
@has_permission_or_is_staff("change_donation")
def donation_quick_edit(request: HttpRequest, donation_id: int) -> HttpResponse:
    """Quick edit endpoint for QA batch review modal.

    Updates only essential donation fields: amount, currency, payment_method,
    donation_date, and gift_aid.

    Returns JSON for AJAX or redirects for regular form submission.
    """
    donation = get_object_or_404(Donation, pk=donation_id)

    # Extract fields from POST data
    amount = request.POST.get("amount", "").strip()
    currency = request.POST.get("currency", "").strip()
    payment_method = request.POST.get("payment_method", "").strip()
    gift_aid = request.POST.get("gift_aid") == "on"

    errors = []

    # Validate amount
    try:
        amount_float = float(amount) if amount else 0
        if amount_float <= 0:
            errors.append("Amount must be greater than 0")
    except ValueError:
        errors.append("Invalid amount format")
        amount_float = donation.amount

    # If there are errors, return them
    if errors:
        # Check if AJAX request
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": False, "errors": errors}, status=400)

        # Regular form submission
        for error in errors:
            messages.error(request, error)
        return redirect(request.META.get("HTTP_REFERER", "/"))

    # Update donation fields
    donation.amount = amount_float
    donation.currency = currency
    donation.payment_method = payment_method
    donation.donation_date = timezone.now().date()
    donation.gift_aid = gift_aid
    donation.save(
        update_fields=[
            "amount",
            "currency",
            "payment_method",
            "donation_date",
            "gift_aid",
            "updated_at",
        ]
    )

    # Check if AJAX request
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {
                "success": True,
                "message": "Donation updated successfully",
                "donation": {
                    "id": donation.id,
                    "amount": float(donation.amount),
                    "currency": donation.currency,
                    "payment_method": donation.get_payment_method_display(),
                    "donation_date": donation.donation_date.strftime("%d/%m/%Y"),
                    "gift_aid": donation.gift_aid,
                },
            }
        )

    # Regular form submission - redirect back with success message
    messages.success(request, "Donation updated successfully")
    return redirect(request.META.get("HTTP_REFERER", "/"))
