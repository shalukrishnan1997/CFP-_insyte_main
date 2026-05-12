"""Letter admin views: print console, per-campaign workspace, and batch management."""

import logging
import mimetypes
import os
from io import BytesIO
from typing import Any
from uuid import UUID

from celery.result import AsyncResult
from django.conf import settings
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from docxtpl import DocxTemplate

from audit.utils import log_request_action
from campaigns.models import Campaign
from clients.models import Client
from core.storage_helpers import (
    local_storage_path,
    media_storage_exists,
    normalize_media_storage_name,
    open_media_storage_file,
)
from custom_admin.forms import LetterTemplateUploadForm
from donations.models import Donation
from letters.models import LetterBatch, LetterTemplate
from letters.tasks import (
    MAX_LETTERS_PER_FILE,
    build_letter_generation_queryset,
    cancel_letter_batch,
    generate_letter_batch_task,
    get_jinja_env,
    reset_failed_donations,
)
from letters.view_helpers import (
    build_generated_letters_context,
    build_generation_values,
    build_letter_generation_error_response,
    build_letter_generation_success_response,
    build_letter_setup_context,
    build_template_upload_error_response,
    build_template_validation_context,
    collect_generated_letters,
    enqueue_form_errors,
    get_eligible_donation_counts,
    inspect_docx_template,
)
from responsehandling.permissions import is_authenticated_and_is_staff

logger = logging.getLogger(__name__)


def _redirect_to_campaign_section(campaign: Campaign, section: str) -> HttpResponse:
    """Redirect to the campaign workspace with a focused section."""
    campaign_url = reverse(
        "custom_admin:letter_setup_campaign", kwargs={"campaign_id": campaign.id}
    )
    return redirect(f"{campaign_url}?section={section}")


def _next_batch_number(campaign: Campaign) -> int:
    last_batch = (
        LetterBatch.objects.filter(campaign=campaign).order_by("-batch_number").first()
    )
    return (last_batch.batch_number + 1) if last_batch else 1


def _queue_letter_batch(
    *,
    campaign: Campaign,
    thanks_template: LetterTemplate,
    issue_template: LetterTemplate | None,
    total_letters: int,
    letters_per_file: int,
    donation_filter: str,
    regenerate_mode: bool,
    user: Any,
) -> tuple[LetterBatch, str]:
    """Create a LetterBatch and enqueue it for async processing."""
    with transaction.atomic():
        # Lock the campaign row so concurrent generate requests serialize
        # on _next_batch_number() and can't collide on the
        # (campaign, batch_number) unique constraint.
        Campaign.objects.select_for_update().get(pk=campaign.pk)
        batch = LetterBatch.objects.create(
            campaign=campaign,
            template=thanks_template,
            failure_template=issue_template,
            batch_number=_next_batch_number(campaign),
            status=LetterBatch.STATUS_PENDING,
            total_letters=total_letters,
            letters_per_file=min(letters_per_file, MAX_LETTERS_PER_FILE),
            donation_filter=donation_filter,
            regenerate_mode=regenerate_mode,
            created_by=user,
        )
    task = generate_letter_batch_task.delay(str(batch.id))
    batch.celery_task_id = task.id
    batch.save(update_fields=["celery_task_id"])
    return batch, task.id


# ---------------------------------------------------------------------------
#  Print operator console
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def letter_print_console(request: HttpRequest) -> HttpResponse:
    """Single page for the print operator: pick a client + campaign and generate."""
    clients = Client.objects.filter(is_active=True).order_by("name")

    client_id = request.GET.get("client")
    campaign_id = request.GET.get("campaign")

    selected_client = None
    if client_id:
        selected_client = Client.objects.filter(id=client_id, is_active=True).first()

    campaigns = (
        Campaign.objects.filter(client=selected_client).order_by("name")
        if selected_client
        else Campaign.objects.none()
    )

    selected_campaign: Campaign | None = None
    if selected_client and campaign_id:
        selected_campaign = campaigns.filter(id=campaign_id).first()

    counts = (
        get_eligible_donation_counts(selected_campaign)
        if selected_campaign
        else {
            "approved_pending": 0,
            "rejected_pending": 0,
            "approved_total": 0,
            "rejected_total": 0,
        }
    )

    active_thanks = active_issue = None
    recent_batches: list[LetterBatch] = []
    if selected_campaign:
        active_thanks = LetterTemplate.objects.filter(
            campaign=selected_campaign,
            template_type=LetterTemplate.TEMPLATE_TYPE_THANK_YOU,
            is_active=True,
        ).first()
        active_issue = LetterTemplate.objects.filter(
            campaign=selected_campaign,
            template_type=LetterTemplate.TEMPLATE_TYPE_ISSUE,
            is_active=True,
        ).first()
        recent_batches = list(
            LetterBatch.objects.filter(campaign=selected_campaign).order_by(
                "-created_at"
            )[:5]
        )

    context = {
        "clients": clients,
        "campaigns": campaigns,
        "selected_client": selected_client,
        "selected_campaign": selected_campaign,
        "active_thanks_template": active_thanks,
        "active_issue_template": active_issue,
        "approved_pending": counts["approved_pending"],
        "rejected_pending": counts["rejected_pending"],
        "approved_total": counts["approved_total"],
        "rejected_total": counts["rejected_total"],
        "recent_batches": recent_batches,
        "active": "letter_setup",
        "breadcrumbs": [{"name": "Letters", "url": None}],
    }
    return render(request, "admin/letter_print_console.html", context)


# ---------------------------------------------------------------------------
#  Campaign admin workspace (template uploads + detailed view)
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def letter_setup_campaign(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """Per-campaign admin page for managing templates and viewing batches."""
    campaign = get_object_or_404(Campaign, id=campaign_id)
    context = build_letter_setup_context(
        campaign, focus_section=request.GET.get("section", "templates")
    )
    return render(request, "admin/letter_setup_campaign.html", context)


@is_authenticated_and_is_staff
def add_letter_template(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """Upload a new letter template; deactivates the previous active one for its type."""
    campaign = get_object_or_404(Campaign, id=campaign_id)

    if request.method != "POST":
        return _redirect_to_campaign_section(campaign, "upload")

    form = LetterTemplateUploadForm(request.POST or None, request.FILES or None)

    if not form.is_valid():
        enqueue_form_errors(request, form)
        return build_template_upload_error_response(request, campaign, form)

    uploaded_file = form.cleaned_data["file"]
    template_name = form.cleaned_data.get("template_name") or "Template"
    template_type = (
        form.cleaned_data.get("template_type") or LetterTemplate.TEMPLATE_TYPE_THANK_YOU
    )
    file_bytes = uploaded_file.read()

    if not file_bytes:
        return build_template_upload_error_response(
            request, campaign, form, error_message="Uploaded file is empty."
        )

    try:
        unknown_placeholders, detected_placeholders = inspect_docx_template(file_bytes)

        for variable_name in unknown_placeholders:
            messages.warning(
                request,
                f"Unsupported template variable detected: {{{{{variable_name}}}}}",
            )

        with transaction.atomic():
            LetterTemplate.objects.filter(
                campaign=campaign, template_type=template_type, is_active=True
            ).update(is_active=False)

            LetterTemplate.objects.create(
                campaign=campaign,
                name=template_name,
                template_type=template_type,
                file=ContentFile(file_bytes, name=uploaded_file.name),
                is_active=True,
                created_by=request.user,
            )

        messages.success(
            request,
            (
                f'Template "{uploaded_file.name}" uploaded successfully. '
                f"Detected {len(detected_placeholders)} template variables."
            ),
        )
        return _redirect_to_campaign_section(campaign, "templates")
    except Exception as exc:
        logger.exception("Error processing template upload: %s", exc)
        return build_template_upload_error_response(
            request,
            campaign,
            form,
            error_message=(
                "Template validation failed. DOCX files can use docxtpl variables and "
                f"conditions, but the syntax must be valid. Details: {exc!s}"
            ),
        )


# ---------------------------------------------------------------------------
#  Generate letters
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def generate_letter(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """Queue a new letter batch for the campaign.

    Thanks and issue templates are read from the campaign's active templates.
    Approved donations get the thanks template; rejected donations get the
    issue template (skipped if no issue template is configured).
    """
    campaign = get_object_or_404(Campaign, id=campaign_id)

    if request.method != "POST":
        return _redirect_to_campaign_section(campaign, "generate")

    generation_values = build_generation_values(request.POST)
    donation_filter = request.POST.get("donation_filter", "all")
    regenerate_mode = request.POST.get("regenerate_mode") == "on"

    try:
        letters_per_file = int(request.POST.get("letters_per_file", 100))
    except TypeError, ValueError:
        return build_letter_generation_error_response(
            request,
            campaign,
            "Letters per file must be a whole number.",
            generation_values,
        )

    thanks_template = LetterTemplate.objects.filter(
        campaign=campaign,
        template_type=LetterTemplate.TEMPLATE_TYPE_THANK_YOU,
        is_active=True,
    ).first()
    if not thanks_template:
        return build_letter_generation_error_response(
            request,
            campaign,
            "Upload a thank-you template for this campaign before generating letters.",
            generation_values,
        )

    issue_template = LetterTemplate.objects.filter(
        campaign=campaign,
        template_type=LetterTemplate.TEMPLATE_TYPE_ISSUE,
        is_active=True,
    ).first()

    selected_qs = build_letter_generation_queryset(
        campaign=campaign,
        donation_filter=donation_filter,
        regenerate_mode=regenerate_mode,
    )
    if issue_template is None:
        # Rejected donations are skipped at generation time when no issue
        # template is configured. Exclude them from the count so the progress
        # bar denominator matches what will actually be produced.
        selected_qs = selected_qs.filter(qa_status=Donation.QA_STATUS_APPROVED)
    selected_donations = selected_qs.count()

    if selected_donations == 0:
        return build_letter_generation_error_response(
            request,
            campaign,
            "No approved or rejected donations match the selected filters.",
            generation_values,
        )

    try:
        batch, task_id = _queue_letter_batch(
            campaign=campaign,
            thanks_template=thanks_template,
            issue_template=issue_template,
            total_letters=selected_donations,
            letters_per_file=letters_per_file,
            donation_filter=donation_filter,
            regenerate_mode=regenerate_mode,
            user=request.user,
        )
        return build_letter_generation_success_response(
            request, campaign, batch, task_id, selected_donations
        )
    except Exception as exc:
        logger.exception("Failed to start letter generation: %s", exc)
        return build_letter_generation_error_response(
            request,
            campaign,
            "Failed to start letter generation. Please try again.",
            generation_values,
            status=500,
        )


DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
REFERENCE_GUIDE_PATH = os.path.join(
    settings.BASE_DIR,
    "docs",
    "letter_templates",
    "letter_template_field_reference.docx",
)


@is_authenticated_and_is_staff
def serve_reference_guide(request: HttpRequest) -> FileResponse:
    """Serve the letter-template authoring reference DOCX."""
    if not os.path.exists(REFERENCE_GUIDE_PATH):
        raise Http404("Reference guide not found")
    # FileResponse takes ownership of the file handle and closes it after send.
    response = FileResponse(
        open(REFERENCE_GUIDE_PATH, "rb"),  # noqa: SIM115
        content_type=DOCX_CONTENT_TYPE,
    )
    response["Content-Disposition"] = (
        'attachment; filename="letter_template_field_reference.docx"'
    )
    return response


@is_authenticated_and_is_staff
def preview_active_template(
    request: HttpRequest, campaign_id: UUID, template_type: str
) -> FileResponse | HttpResponse:
    """Render the active template with sample data and return the resulting DOCX."""
    campaign = get_object_or_404(Campaign, id=campaign_id)
    if template_type not in (
        LetterTemplate.TEMPLATE_TYPE_THANK_YOU,
        LetterTemplate.TEMPLATE_TYPE_ISSUE,
    ):
        raise Http404("Unknown template type")

    template = LetterTemplate.objects.filter(
        campaign=campaign, template_type=template_type, is_active=True
    ).first()
    if template is None:
        messages.error(
            request,
            f"No active {template_type.replace('_', ' ')} template to preview.",
        )
        return _redirect_to_campaign_section(campaign, "templates")

    buffer = BytesIO()
    try:
        with local_storage_path(template.file) as template_path:
            doc = DocxTemplate(template_path)
            detected = doc.get_undeclared_template_variables(context={}) or []
            context = build_template_validation_context(set(detected))
            doc.render(context, jinja_env=get_jinja_env())
            doc.save(buffer)
    except Exception as exc:
        logger.exception("Failed to preview template %s: %s", template.id, exc)
        messages.error(
            request,
            f"Preview render failed. Check template syntax. Details: {exc!s}",
        )
        return _redirect_to_campaign_section(campaign, "templates")

    buffer.seek(0)
    safe_campaign = "".join(c if c.isalnum() else "_" for c in campaign.name)
    filename = f"{safe_campaign}_{template_type}_sample.docx"
    response = FileResponse(buffer, content_type=DOCX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
#  Generated file listing + downloads
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def view_generated_letters(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """View all generated letters for a campaign with pagination."""
    campaign = get_object_or_404(Campaign, id=campaign_id)
    letters = collect_generated_letters(campaign_id)
    return render(
        request,
        "admin/generated_letters.html",
        build_generated_letters_context(request, campaign, letters),
    )


@is_authenticated_and_is_staff  # pyright: ignore[reportArgumentType]
def download_letter(
    request: HttpRequest, campaign_id: UUID, filename: str
) -> FileResponse:
    """Download a generated letter file by filename."""
    get_object_or_404(Campaign, id=campaign_id)

    storage_name = normalize_media_storage_name(
        f"generated_letters/{campaign_id}/{filename}"
    )
    if not media_storage_exists(storage_name):
        raise Http404("Letter not found")

    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"

    campaign = Campaign.objects.get(id=campaign_id)
    log_request_action(
        request,
        action="DOWNLOAD",
        model_name="Letter",
        object_id=str(campaign_id),
        object_repr=f"Letter: {filename}",
        summary=f"Downloaded letter {filename} from campaign {campaign.name}",
    )

    response = FileResponse(
        open_media_storage_file(storage_name), content_type=mime_type
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
#  Batch management + task status
# ---------------------------------------------------------------------------


@is_authenticated_and_is_staff
def letter_task_status(request: HttpRequest, task_id: str) -> JsonResponse:
    """Return the current state of a Celery letter task (for UI polling)."""
    task_result = AsyncResult(task_id)

    response_data: dict[str, Any] = {
        "task_id": task_id,
        "status": task_result.state,
    }

    if task_result.state == "SUCCESS":
        result = task_result.result or {}
        response_data.update(
            {
                "success": result.get("success", False),
                "total_donations": result.get("total_donations", 0),
                "generated_count": result.get("generated_count", 0),
                "failed_count": result.get("failed_count", 0),
                "errors": result.get("errors", []),
            }
        )
    elif task_result.state == "FAILURE":
        response_data["error"] = str(task_result.info)
    elif task_result.state == "PENDING":
        response_data["message"] = "Task is waiting to be processed"
    elif task_result.state == "STARTED":
        response_data["message"] = "Task is currently processing"

    return JsonResponse(response_data)


@is_authenticated_and_is_staff
def letter_batches(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """List all letter batches for a campaign."""
    campaign = get_object_or_404(Campaign, id=campaign_id)
    batches = LetterBatch.objects.filter(campaign=campaign).order_by("-created_at")

    donations_qs = Donation.objects.filter(campaign=campaign)
    stats = {
        "total": donations_qs.count(),
        "pending": donations_qs.filter(letter_status="pending").count(),
        "generated": donations_qs.filter(letter_status="generated").count(),
        "failed": donations_qs.filter(letter_status="failed").count(),
    }

    context = {
        "campaign": campaign,
        "batches": batches,
        "stats": stats,
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
            {"name": "Letter Batches", "url": None},
        ],
    }
    return render(request, "admin/letter_batches.html", context)


@is_authenticated_and_is_staff
def letter_batch_detail(request: HttpRequest, batch_id: str) -> HttpResponse:
    """Show progress, generated files, and errors for one batch."""
    batch = get_object_or_404(
        LetterBatch.objects.select_related("campaign", "template", "created_by"),
        id=batch_id,
    )

    donations = (
        Donation.objects.filter(letter_batch=batch)
        .select_related("donor", "data_file_donor", "campaign")
        .order_by("created_at")[:100]
    )

    context = {
        "batch": batch,
        "campaign": batch.campaign,
        "donations": donations,
        "active": "letter_setup",
        "breadcrumbs": [
            {"name": "Letters", "url": reverse("custom_admin:letter_print_console")},
            {
                "name": batch.campaign.name,
                "url": reverse(
                    "custom_admin:letter_setup_campaign",
                    kwargs={"campaign_id": batch.campaign.id},
                ),
            },
            {
                "name": "Letter Batches",
                "url": reverse(
                    "custom_admin:letter_batches",
                    kwargs={"campaign_id": batch.campaign.id},
                ),
            },
            {"name": f"Batch #{batch.batch_number}", "url": None},
        ],
    }
    return render(request, "admin/letter_batch_detail.html", context)


@is_authenticated_and_is_staff
def letter_batch_status_api(request: HttpRequest, batch_id: str) -> JsonResponse:
    """JSON API for batch progress polling."""
    try:
        batch = LetterBatch.objects.get(id=batch_id)
    except LetterBatch.DoesNotExist:
        return JsonResponse({"success": False, "error": "Batch not found"}, status=404)

    task_status = None
    if batch.celery_task_id:
        task_result = AsyncResult(batch.celery_task_id)
        task_status = task_result.state
        if task_result.state == "PROGRESS":  # pyright: ignore[reportUnnecessaryComparison]
            meta = task_result.info or {}
            batch.progress_percent = meta.get("percent", batch.progress_percent)

    return JsonResponse(
        {
            "success": True,
            "batch_id": str(batch.id),
            "status": batch.status,
            "progress_percent": batch.progress_percent,
            "total_letters": batch.total_letters,
            "generated_count": batch.generated_count,
            "failed_count": batch.failed_count,
            "file_count": batch.file_count,
            "task_status": task_status,
            "started_at": batch.started_at.isoformat() if batch.started_at else None,
            "completed_at": (
                batch.completed_at.isoformat() if batch.completed_at else None
            ),
        }
    )


@is_authenticated_and_is_staff
def cancel_batch(request: HttpRequest, batch_id: str) -> HttpResponse:
    """Cancel a running/pending letter batch."""
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "error": "Method not allowed"}, status=405
        )

    try:
        batch = LetterBatch.objects.get(id=batch_id)
    except LetterBatch.DoesNotExist:
        return JsonResponse({"success": False, "error": "Batch not found"}, status=404)

    if batch.status in ("completed", "cancelled"):
        return JsonResponse(
            {"success": False, "error": f"Batch already {batch.status}"}, status=400
        )

    cancel_letter_batch.delay(str(batch_id))

    if (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.content_type == "application/json"
    ):
        return JsonResponse(
            {"success": True, "message": "Batch cancellation requested"}
        )

    messages.success(request, "Batch cancellation requested")
    return redirect("custom_admin:letter_batches", campaign_id=batch.campaign.id)


@is_authenticated_and_is_staff  # pyright: ignore[reportArgumentType]
def download_batch_file(
    request: HttpRequest, batch_id: str, file_index: int
) -> FileResponse:
    """Download a specific output file from a batch by its index."""
    batch = get_object_or_404(LetterBatch, id=batch_id)

    if not batch.output_files or file_index >= len(batch.output_files):
        raise Http404("File not found")

    stored_path = str(batch.output_files[file_index])
    storage_name = normalize_media_storage_name(stored_path)
    if not media_storage_exists(storage_name):
        raise Http404("File not found")

    mime_type, _ = mimetypes.guess_type(storage_name)
    if not mime_type:
        mime_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    filename = os.path.basename(storage_name)
    response = FileResponse(
        open_media_storage_file(storage_name), content_type=mime_type
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@is_authenticated_and_is_staff
def reset_failed_letters(request: HttpRequest, campaign_id: UUID) -> HttpResponse:
    """Reset failed donations' letter_status to pending so they can be retried."""
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "error": "Method not allowed"}, status=405
        )

    campaign = get_object_or_404(Campaign, id=campaign_id)
    reset_failed_donations.delay(str(campaign.id))

    if (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.content_type == "application/json"
    ):
        return JsonResponse({"success": True, "message": "Reset initiated"})

    messages.success(
        request, "Failed letters reset to pending. You can now regenerate them."
    )
    return redirect("custom_admin:letter_batches", campaign_id=campaign.id)
