"""Helpers shared by the letters admin views."""

from datetime import date, datetime
from io import BytesIO
from typing import Any

from django.contrib import messages
from django.core.paginator import Page
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from docxtpl import DocxTemplate

from campaigns.models import Campaign
from core.pagination import paginate_queryset
from core.storage_helpers import (
    media_storage_exists,
    media_storage_modified_time,
    media_storage_size,
    normalize_media_storage_name,
)
from custom_admin.forms import LetterTemplateUploadForm
from donations.models import Donation
from letters.models import LetterBatch, LetterTemplate
from letters.tasks import get_jinja_env

AVAILABLE_PLACEHOLDERS: dict[str, list[dict[str, str]]] = {
    "donor": [
        {
            "placeholder": "{{donor_title}}",
            "field": "donor.title",
            "description": "Donor title (Mr, Mrs, Dr, etc.)",
        },
        {
            "placeholder": "{{donor_first_name}}",
            "field": "donor.first_name",
            "description": "Donor first name",
        },
        {
            "placeholder": "{{donor_last_name}}",
            "field": "donor.last_name",
            "description": "Donor last name",
        },
        {
            "placeholder": "{{donor_full_name}}",
            "field": "donor.full_name",
            "description": "Full donor name",
        },
        {
            "placeholder": "{{donor_email}}",
            "field": "donor.email",
            "description": "Donor email address",
        },
        {
            "placeholder": "{{donor_phone}}",
            "field": "donor.phone",
            "description": "Donor phone number",
        },
        {
            "placeholder": "{{donor_address_line1}}",
            "field": "donor.address_line1",
            "description": "Address line 1",
        },
        {
            "placeholder": "{{donor_address_line2}}",
            "field": "donor.address_line2",
            "description": "Address line 2",
        },
        {"placeholder": "{{donor_city}}", "field": "donor.city", "description": "City"},
        {
            "placeholder": "{{donor_county}}",
            "field": "donor.county",
            "description": "County",
        },
        {
            "placeholder": "{{donor_postcode}}",
            "field": "donor.postcode",
            "description": "Postcode",
        },
    ],
    "donation": [
        {
            "placeholder": "{{amount}}",
            "field": "donation.amount",
            "description": "Donation amount (number only)",
        },
        {
            "placeholder": "{{amount_formatted}}",
            "field": "donation.amount_formatted",
            "description": "Donation amount with currency symbol",
        },
        {
            "placeholder": "{{amount_raw}}",
            "field": "donation.amount_raw",
            "description": "Raw numeric donation amount for comparisons",
        },
        {
            "placeholder": "{{currency_symbol}}",
            "field": "donation.currency_symbol",
            "description": "Currency symbol",
        },
        {
            "placeholder": "{{payment_method}}",
            "field": "donation.payment_method",
            "description": "Payment method",
        },
        {
            "placeholder": "{{payment_status}}",
            "field": "donation.payment_status",
            "description": "Payment status for conditions",
        },
        {
            "placeholder": "{{gift_aid}}",
            "field": "donation.gift_aid",
            "description": "Gift Aid flag (Yes or No)",
        },
        {
            "placeholder": "{{donation_date}}",
            "field": "donation.donation_date",
            "description": "Formatted donation date",
        },
        {
            "placeholder": "{{donation_date_obj}}",
            "field": "donation.donation_date_obj",
            "description": "Raw donation date object for date filters",
        },
        {
            "placeholder": "{{donation_reference}}",
            "field": "donation.id",
            "description": "Unique donation reference",
        },
    ],
    "campaign": [
        {
            "placeholder": "{{campaign_name}}",
            "field": "campaign.name",
            "description": "Campaign name",
        },
        {
            "placeholder": "{{campaign_description}}",
            "field": "campaign.description",
            "description": "Campaign description",
        },
        {
            "placeholder": "{{appeal_code}}",
            "field": "campaign.appeal_code",
            "description": "Campaign appeal code",
        },
    ],
    "client": [
        {
            "placeholder": "{{client_name}}",
            "field": "client.name",
            "description": "Organisation name",
        },
        {
            "placeholder": "{{client_email}}",
            "field": "client.email",
            "description": "Organisation email",
        },
        {
            "placeholder": "{{client_phone}}",
            "field": "client.phone",
            "description": "Organisation phone",
        },
        {
            "placeholder": "{{client_address_line1}}",
            "field": "client.address_line1",
            "description": "Organisation address line 1",
        },
        {
            "placeholder": "{{client_city}}",
            "field": "client.city",
            "description": "Organisation city",
        },
        {
            "placeholder": "{{client_postcode}}",
            "field": "client.postal_code",
            "description": "Organisation postcode",
        },
    ],
    "other": [
        {
            "placeholder": "{{date}}",
            "field": "generated_date",
            "description": "Letter generation date",
        },
        {
            "placeholder": "{{year}}",
            "field": "generated_year",
            "description": "Current year",
        },
        {
            "placeholder": "{{month}}",
            "field": "generated_month",
            "description": "Current month name",
        },
        {
            "placeholder": "{{day}}",
            "field": "generated_day",
            "description": "Current day of month",
        },
    ],
}


def is_ajax_request(request: HttpRequest) -> bool:
    """Return whether the request expects a JSON response."""
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.content_type == "application/json"
    )


def get_available_placeholders() -> dict[str, list[dict[str, str]]]:
    """Return the supported template variables grouped for the UI."""
    return AVAILABLE_PLACEHOLDERS


def _known_placeholder_names() -> set[str]:
    """Return the set of placeholder variable names known to the system."""
    return {
        item["placeholder"].removeprefix("{{").removesuffix("}}")
        for items in AVAILABLE_PLACEHOLDERS.values()
        for item in items
    }


def build_template_validation_context(variable_names: set[str]) -> dict[str, Any]:
    """Return a permissive context used to render-test an uploaded template."""
    sample_date = date(2026, 3, 12)
    context: dict[str, Any] = {
        "donor_title": "Ms",
        "donor_first_name": "Alex",
        "donor_last_name": "Taylor",
        "donor_full_name": "Ms Alex Taylor",
        "donor_email": "alex@example.org",
        "donor_phone": "07123456789",
        "donor_address_line1": "1 High Street",
        "donor_address_line2": "Flat 2",
        "donor_city": "London",
        "donor_county": "Greater London",
        "donor_postcode": "SW1A 1AA",
        "amount": "125.00",
        "amount_formatted": "£125.00",
        "amount_raw": 125.0,
        "currency_symbol": "£",
        "payment_method": "Direct Debit",
        "payment_status": "completed",
        "gift_aid": "Yes",
        "donation_date": "12 March 2026",
        "donation_date_obj": sample_date,
        "donation_reference": "ABC12345",
        "campaign_name": "Spring Appeal",
        "campaign_description": "Campaign description",
        "appeal_code": "SPRING26",
        "client_name": "Example Charity",
        "client_email": "hello@example.org",
        "client_phone": "02070000000",
        "client_address_line1": "10 Charity House",
        "client_city": "London",
        "client_postcode": "N1 1AA",
        "date": "12 March 2026",
        "year": "2026",
        "month": "March",
        "day": "12",
    }

    numeric_markers = ("amount", "count", "total", "number", "index")
    boolean_markers = ("is_", "has_", "show_", "allow_", "enable_", "display_")
    for variable_name in variable_names:
        if variable_name in context:
            continue
        if variable_name.endswith("_date_obj"):
            context[variable_name] = sample_date
        elif variable_name.startswith(boolean_markers):
            context[variable_name] = True
        elif any(marker in variable_name for marker in numeric_markers):
            context[variable_name] = 1
        else:
            context[variable_name] = "Sample"
    return context


def inspect_docx_template(file_bytes: bytes) -> tuple[list[str], list[str]]:
    """Validate a DOCX template by rendering it against a sample context.

    Returns:
        Tuple of (unknown_variable_names, detected_variable_names).
        ``unknown_variable_names`` lists variables the operator used but the
        system doesn't know how to fill.
    """
    inspector = DocxTemplate(BytesIO(file_bytes))
    detected = sorted(
        set(inspector.get_undeclared_template_variables(context={}) or [])
    )

    validator = DocxTemplate(BytesIO(file_bytes))
    validator.render(
        build_template_validation_context(set(detected)), jinja_env=get_jinja_env()
    )

    known = _known_placeholder_names()
    unknown = [name for name in detected if name not in known]
    return unknown, detected


def build_generation_values(source: Any | None = None) -> dict[str, Any]:
    """Normalize generate-letter form values for re-display on errors."""
    data: dict[str, Any] = source or {}
    regenerate_value = data.get("regenerate_mode")
    return {
        "letters_per_file": str(data.get("letters_per_file") or "100"),
        "donation_filter": str(data.get("donation_filter") or "all"),
        "regenerate_mode": regenerate_value in {True, "true", "True", "on", "1"},
    }


def get_eligible_donation_counts(campaign: Campaign) -> dict[str, int]:
    """Return counts for the donations currently eligible for letter generation."""
    approved = Q(qa_status=Donation.QA_STATUS_APPROVED)
    rejected = Q(qa_status=Donation.QA_STATUS_REJECTED)
    pending = Q(letter_status="pending")
    aggregates = Donation.objects.filter(
        campaign=campaign,
        qa_status__in=[Donation.QA_STATUS_APPROVED, Donation.QA_STATUS_REJECTED],
    ).aggregate(
        approved_pending=Count("id", filter=approved & pending),
        rejected_pending=Count("id", filter=rejected & pending),
        approved_total=Count("id", filter=approved),
        rejected_total=Count("id", filter=rejected),
    )
    return {key: value or 0 for key, value in aggregates.items()}


def build_letter_setup_context(
    campaign: Campaign,
    *,
    upload_form: LetterTemplateUploadForm | None = None,
    generation_values: dict[str, Any] | None = None,
    focus_section: str = "templates",
    detected_placeholders: list[str] | None = None,
    unknown_placeholders: list[str] | None = None,
) -> dict[str, Any]:
    """Context for the campaign letter admin page (template upload + batches)."""
    templates = LetterTemplate.objects.filter(campaign=campaign).order_by("-created_at")
    donations_qs = Donation.objects.filter(campaign=campaign)
    counts = get_eligible_donation_counts(campaign)

    if focus_section not in {"templates", "upload", "generate", "batches"}:
        focus_section = "templates"

    active_thanks = templates.filter(
        template_type=LetterTemplate.TEMPLATE_TYPE_THANK_YOU, is_active=True
    ).first()
    active_issue = templates.filter(
        template_type=LetterTemplate.TEMPLATE_TYPE_ISSUE, is_active=True
    ).first()

    return {
        "campaign": campaign,
        "templates": templates,
        "thanks_templates": templates.filter(
            template_type=LetterTemplate.TEMPLATE_TYPE_THANK_YOU
        ),
        "issue_templates": templates.filter(
            template_type=LetterTemplate.TEMPLATE_TYPE_ISSUE
        ),
        "active_thanks_template": active_thanks,
        "active_issue_template": active_issue,
        "upload_form": upload_form or LetterTemplateUploadForm(),
        "generation_values": generation_values or build_generation_values(),
        "total_donations": donations_qs.count(),
        "approved_pending": counts["approved_pending"],
        "rejected_pending": counts["rejected_pending"],
        "approved_total": counts["approved_total"],
        "rejected_total": counts["rejected_total"],
        "pending_donations": counts["approved_pending"] + counts["rejected_pending"],
        "generated_donations": donations_qs.filter(letter_status="generated").count(),
        "failed_donations": donations_qs.filter(letter_status="failed").count(),
        "batches": LetterBatch.objects.filter(campaign=campaign).order_by(
            "-created_at"
        )[:5],
        "available_placeholders": get_available_placeholders(),
        "template_type_choices": LetterTemplate.TEMPLATE_TYPE_CHOICES,
        "upload_detected_placeholders": detected_placeholders or [],
        "upload_unknown_placeholders": unknown_placeholders or [],
        "focus_section": focus_section,
        "active": "letter_setup",
        "breadcrumbs": [
            {"name": "Letters", "url": reverse("custom_admin:letter_print_console")},
            {"name": campaign.client.name, "url": None},
            {"name": campaign.name, "url": None},
        ],
    }


def render_letter_setup_workspace(
    request: HttpRequest,
    campaign: Campaign,
    *,
    upload_form: LetterTemplateUploadForm | None = None,
    generation_values: dict[str, Any] | None = None,
    focus_section: str = "templates",
) -> HttpResponse:
    """Render the campaign letter admin page with the requested section active."""
    return render(
        request,
        "admin/letter_setup_campaign.html",
        build_letter_setup_context(
            campaign,
            upload_form=upload_form,
            generation_values=generation_values,
            focus_section=focus_section,
        ),
    )


def enqueue_form_errors(request: HttpRequest, form: LetterTemplateUploadForm) -> None:
    """Surface form validation errors via the Django messages framework."""
    for field_errors in form.errors.values():
        for error in field_errors:
            messages.error(request, str(error))


def build_template_upload_error_response(
    request: HttpRequest,
    campaign: Campaign,
    form: LetterTemplateUploadForm,
    *,
    error_message: str | None = None,
) -> HttpResponse:
    """Re-render the upload section with any validation message preserved."""
    if error_message:
        messages.error(request, error_message)
    return render_letter_setup_workspace(
        request, campaign, upload_form=form, focus_section="upload"
    )


def build_letter_generation_error_response(
    request: HttpRequest,
    campaign: Campaign,
    error_message: str,
    generation_values: dict[str, Any],
    *,
    status: int = 400,
) -> HttpResponse:
    """Shared error response for letter-generation failures."""
    if is_ajax_request(request):
        return JsonResponse({"success": False, "error": error_message}, status=status)
    messages.error(request, error_message)
    return render_letter_setup_workspace(
        request, campaign, generation_values=generation_values, focus_section="generate"
    )


def build_letter_generation_success_response(
    request: HttpRequest,
    campaign: Campaign,
    batch: LetterBatch,
    task_id: str,
    total_letters: int,
) -> HttpResponse:
    """Shared success response after queuing a letter batch."""
    batches_url = (
        f"{reverse('custom_admin:letter_setup_campaign', kwargs={'campaign_id': campaign.id})}"
        "?section=batches"
    )
    if is_ajax_request(request):
        return JsonResponse(
            {
                "success": True,
                "task_id": task_id,
                "batch_id": str(batch.id),
                "batch_number": batch.batch_number,
                "campaign_id": str(campaign.id),
                "total_letters": total_letters,
                "redirect_url": batches_url,
            }
        )
    messages.success(
        request,
        f"Letter generation started. Processing {total_letters} donations in the background.",
    )
    return redirect(batches_url)


def collect_generated_letters(campaign_id: object) -> list[dict[str, Any]]:
    """Return file metadata for every generated letter across the campaign's batches."""
    letters: list[dict[str, Any]] = []
    seen_storage_names: set[str] = set()

    batches = LetterBatch.objects.filter(campaign_id=campaign_id).only("output_files")
    for batch in batches:
        for stored_path in batch.output_files or []:
            storage_name = normalize_media_storage_name(str(stored_path))
            if not storage_name or storage_name in seen_storage_names:
                continue
            if not media_storage_exists(storage_name):
                continue

            filename = storage_name.rsplit("/", 1)[-1]
            parts = filename.split("_")
            file_size = media_storage_size(storage_name)
            modified_time = media_storage_modified_time(storage_name)
            created = (
                modified_time if isinstance(modified_time, datetime) else datetime.min
            )
            letters.append(
                {
                    "filename": filename,
                    "donor_urn": parts[1] if len(parts) >= 3 else "Unknown",
                    "size": file_size,
                    "size_kb": round(file_size / 1024, 2),
                    "created": created,
                    "extension": filename.split(".")[-1].upper(),
                }
            )
            seen_storage_names.add(storage_name)

    letters.sort(key=lambda item: item["created"], reverse=True)
    return letters


def build_generated_letters_context(
    request: HttpRequest,
    campaign: Campaign,
    letters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Paginate and contextualise the list of generated letter files."""
    letters_page: Page = paginate_queryset(
        letters, request, per_page=10, per_page_param="per_page"
    )
    return {
        "campaign": campaign,
        "letters": letters_page,
        "total_letters": letters_page.paginator.count,
        "per_page": letters_page.paginator.per_page,
        "active": "letter_setup",
        "breadcrumbs": [
            {"name": "Letters", "url": reverse("custom_admin:letter_print_console")},
            {
                "name": campaign.name,
                "url": reverse(
                    "custom_admin:letter_setup_campaign",
                    kwargs={"campaign_id": campaign.id},
                ),
            },
            {"name": "Generated Letters", "url": None},
        ],
    }
