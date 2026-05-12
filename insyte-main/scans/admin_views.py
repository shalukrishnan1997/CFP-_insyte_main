"""Scan processing views for OCR-based donation form handling.

Provides the staff-facing UI for:
- Viewing scan batches and their OCR processing status.
- Triggering OCR processing for uploaded scans.
- Reviewing OCR-extracted data before verification.
- Manually ingesting R2 scan folders (rclone intake path).
"""

import json
import logging
from typing import Any, cast

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import User
from responsehandling.permissions import is_authenticated_and_is_staff

logger = logging.getLogger(__name__)


WARM_QR_UNREADABLE_REASON = "qr_unreadable_for_warm_campaign"
WARM_QR_MALFORMED_REASON = "qr_malformed_for_warm_campaign"
QR_MALFORMED_PAYLOAD_REASON = "qr_malformed_payload"
WARM_RESCAN_REASON = "warm_source_miss_rescan_under_cold_campaign"
MAPPING_DRAFT_PLACEHOLDER = "__map_to_canonical_field__"


def _scan_batch_queryset() -> Any:
    """Return the annotated scan-batch queryset used by dashboard views."""
    from scans.models import ScanBatch, ScanPlaceholder

    return (
        ScanBatch.objects.select_related(
            "campaign__client",
            "campaign",
            "donation_batch",
            "created_by",
        )
        .prefetch_related(
            Prefetch(
                "placeholders",
                queryset=ScanPlaceholder.objects.only("id", "batch_id", "ocr_data"),
            )
        )
        .annotate(
            warm_qr_issue_count=Count(
                "placeholders",
                filter=Q(
                    placeholders__ocr_data__exception_reason__in=[
                        WARM_QR_UNREADABLE_REASON,
                        WARM_QR_MALFORMED_REASON,
                    ]
                ),
            ),
            warm_rescan_count=Count(
                "placeholders",
                filter=Q(
                    placeholders__ocr_data__exception_reason__in=[
                        WARM_RESCAN_REASON,
                        QR_MALFORMED_PAYLOAD_REASON,
                    ]
                ),
            ),
        )
        .order_by("-created_at")
    )


def _attach_dashboard_unmapped_label_stats(scan_batches: list[Any]) -> None:
    """Attach unmapped OCR label counts to dashboard batch rows."""
    for batch in scan_batches:
        placeholders = list(getattr(batch, "placeholders", []).all())
        unmapped_label_summary, unmapped_placeholder_count = _summarize_unmapped_labels(
            placeholders
        )
        batch.unmapped_label_summary = unmapped_label_summary
        batch.unmapped_placeholder_count = unmapped_placeholder_count


DEFAULT_DASHBOARD_PER_PAGE = 50
ALLOWED_STATUS_FILTERS = ("active", "all", "completed")


def _apply_dashboard_status_filter(scan_batches: Any, status_filter: str) -> Any:
    """Apply the status filter to the scan-batch queryset.

    Default ``"active"`` hides batches whose ``status == STATUS_COMPLETED``
    *and* hides batches that have reached 100% progress (``processed_scans >=
    total_scans`` with at least one scan), so operators see only the work
    that still needs attention. ``"completed"`` is the inverse, and
    ``"all"`` is unfiltered.
    """
    from scans.models import ScanBatch

    full_progress_q = Q(total_scans__gt=0) & Q(processed_scans__gte=F("total_scans"))
    if status_filter == "all":
        return scan_batches
    if status_filter == "completed":
        return scan_batches.filter(
            Q(status=ScanBatch.STATUS_COMPLETED) | full_progress_q
        )
    # "active" (default): hide completed and 100%-scanned batches.
    return scan_batches.exclude(status=ScanBatch.STATUS_COMPLETED).exclude(
        full_progress_q
    )


def _get_filtered_scan_batches(
    campaign_id: str,
    attention_filter: str,
    status_filter: str,
    page: int,
    per_page: int,
) -> tuple[list[Any], Any]:
    """Return paginated dashboard scan batches and the page object.

    Pagination is real Django ``Paginator`` — the previous ``[:50]`` slice
    silently dropped the 51st batch onwards. Page numbers come from
    ``?page=N``; out-of-range pages clamp to the last page.

    The ``mapping_gaps`` attention filter still operates on the in-memory
    page (its underlying signal is on a per-batch annotation that's only
    materialised after ``_attach_dashboard_unmapped_label_stats``); that's
    a known limitation of the legacy filter, preserved here.
    """
    scan_batches = _scan_batch_queryset()
    if campaign_id:
        scan_batches = scan_batches.filter(campaign_id=campaign_id)
    if attention_filter == "warm_exceptions":
        scan_batches = scan_batches.filter(
            Q(warm_qr_issue_count__gt=0) | Q(warm_rescan_count__gt=0)
        )
    scan_batches = _apply_dashboard_status_filter(scan_batches, status_filter)
    paginator = Paginator(scan_batches, per_page)
    page_obj = paginator.get_page(page)
    batches = list(page_obj.object_list)
    _attach_dashboard_unmapped_label_stats(batches)
    if attention_filter == "mapping_gaps":
        batches = [batch for batch in batches if batch.unmapped_placeholder_count > 0]
    return batches, page_obj


def _get_warm_attention_counts(scan_batches: list[Any]) -> tuple[int, int]:
    """Summarize the number of batches and records needing warm follow-up."""
    batch_count = sum(
        1
        for batch in scan_batches
        if batch.warm_qr_issue_count > 0 or batch.warm_rescan_count > 0
    )
    record_count = sum(
        batch.warm_qr_issue_count + batch.warm_rescan_count for batch in scan_batches
    )
    return batch_count, record_count


def _get_mapping_gap_counts(scan_batches: list[Any]) -> tuple[int, int]:
    """Summarize batches and scans containing unmapped OCR labels."""
    batch_count = sum(
        1 for batch in scan_batches if batch.unmapped_placeholder_count > 0
    )
    record_count = sum(
        int(getattr(batch, "unmapped_placeholder_count", 0) or 0)
        for batch in scan_batches
    )
    return batch_count, record_count


def _get_dashboard_context(
    campaign_id: str,
    attention_filter: str,
    status_filter: str,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """Build the dashboard context for scan-processing views."""
    from campaigns.models import Campaign

    scan_batches, page_obj = _get_filtered_scan_batches(
        campaign_id, attention_filter, status_filter, page, per_page
    )
    warm_batch_count, warm_record_count = _get_warm_attention_counts(scan_batches)
    mapping_gap_batch_count, mapping_gap_record_count = _get_mapping_gap_counts(
        scan_batches
    )
    campaigns = (
        Campaign.objects.filter(scan_batches__isnull=False)
        .select_related("client")
        .distinct()
        .order_by("-created_at")
    )
    return {
        "active": "scan_processing",
        "scan_batches": scan_batches,
        "campaigns": campaigns,
        "selected_campaign_id": campaign_id,
        "selected_attention_filter": attention_filter,
        "selected_status_filter": status_filter or "active",
        "warm_attention_batch_count": warm_batch_count,
        "warm_attention_record_count": warm_record_count,
        "mapping_gap_batch_count": mapping_gap_batch_count,
        "mapping_gap_record_count": mapping_gap_record_count,
        "page_obj": page_obj,
        "paginator": page_obj.paginator,
        "total_batch_count": page_obj.paginator.count,
        "page_title": "Scan Batches",
    }


def _get_placeholder_review_reason(placeholder: Any) -> str:
    """Return the normalized review exception reason for a placeholder."""
    return str((placeholder.ocr_data or {}).get("exception_reason") or "")


def _get_placeholder_unmapped_labels(placeholder: Any) -> list[str]:
    """Return normalized unmapped OCR labels recorded for a placeholder."""
    raw_labels = (placeholder.ocr_data or {}).get("unmapped_entity_labels", [])
    if not isinstance(raw_labels, list):
        return []
    labels: list[str] = []
    for label in raw_labels:
        if not isinstance(label, str):
            continue
        cleaned = label.strip()
        if cleaned:
            labels.append(cleaned)
    return labels


def _summarize_unmapped_labels(
    placeholders: list[Any],
) -> tuple[list[dict[str, int | str]], int]:
    """Summarize distinct unmapped OCR labels across batch placeholders."""
    label_counts: dict[str, int] = {}
    affected_placeholder_count = 0
    for placeholder in placeholders:
        labels = _get_placeholder_unmapped_labels(placeholder)
        placeholder.unmapped_entity_labels = labels
        if not labels:
            continue
        affected_placeholder_count += 1
        for label in set(labels):
            label_counts[label] = label_counts.get(label, 0) + 1

    summary = [
        {"label": label, "count": count}
        for label, count in sorted(
            label_counts.items(),
            key=lambda item: (-item[1], item[0].lower()),
        )
    ]
    return summary, affected_placeholder_count


def _build_mapping_draft_json(
    unmapped_label_summary: list[dict[str, int | str]],
) -> str:
    """Build a copyable JSON draft for Client.form_field_mapping onboarding."""
    mapping_draft = {
        str(item["label"]): MAPPING_DRAFT_PLACEHOLDER
        for item in unmapped_label_summary
        if isinstance(item.get("label"), str)
    }
    return json.dumps(mapping_draft, indent=2, sort_keys=True)


def _summarize_placeholders(
    placeholders: list[Any],
) -> tuple[dict[str, int], dict[str, int]]:
    """Build per-status and per-exception counts for batch detail pages."""
    from scans.models import ScanPlaceholder

    status_counts = {key: 0 for key, _label in ScanPlaceholder.OCR_STATUS_CHOICES}
    exception_counts = {
        WARM_QR_UNREADABLE_REASON: 0,
        WARM_QR_MALFORMED_REASON: 0,
        QR_MALFORMED_PAYLOAD_REASON: 0,
        WARM_RESCAN_REASON: 0,
    }
    for placeholder in placeholders:
        status_counts[placeholder.ocr_status] = (
            status_counts.get(placeholder.ocr_status, 0) + 1
        )
        review_reason = _get_placeholder_review_reason(placeholder)
        placeholder.review_exception_reason = review_reason
        if review_reason in exception_counts:
            exception_counts[review_reason] += 1
    return status_counts, exception_counts


def _get_scan_batch_detail_context(scan_batch_id: str) -> dict[str, Any]:
    """Build the context for a single scan-batch detail page."""
    from scans.models import ScanBatch, ScanPlaceholder

    scan_batch = get_object_or_404(
        ScanBatch.objects.select_related(
            "campaign__client",
            "campaign",
            "donation_batch",
            "created_by",
        ),
        id=scan_batch_id,
    )
    placeholders = list(
        ScanPlaceholder.objects.filter(batch=scan_batch)
        .select_related("matched_donor", "matched_data_file_donor", "donation")
        .order_by("created_at")
    )
    status_counts, exception_counts = _summarize_placeholders(placeholders)
    unmapped_label_summary, unmapped_placeholder_count = _summarize_unmapped_labels(
        placeholders
    )
    from scans.models import RedactionSettings

    redaction_settings = RedactionSettings.get_settings()
    require_manual_redaction = bool(redaction_settings.required_payment_methods())

    return {
        "active": "scan_processing",
        "scan_batch": scan_batch,
        "placeholders": placeholders,
        "status_counts": status_counts,
        "exception_counts": exception_counts,
        "unmapped_label_summary": unmapped_label_summary,
        "unmapped_placeholder_count": unmapped_placeholder_count,
        "mapping_draft_json": _build_mapping_draft_json(unmapped_label_summary),
        "mapping_draft_placeholder": MAPPING_DRAFT_PLACEHOLDER,
        "page_title": f"Scan Batch: {scan_batch.batch_name}",
        "require_manual_redaction": require_manual_redaction,
    }


def _error_badge(message: str, status: int = 400) -> HttpResponse:
    """Render a standard HTMX error badge response."""
    html = render_to_string(
        "admin/scan_processing/partials/ingest_result_badge.html",
        {"result_status": "error", "message": message, "batches": []},
    )
    return HttpResponse(html, status=status)


def _get_new_batch_post_data(request: HttpRequest) -> dict[str, str]:
    """Normalize HTMX POST fields for new-batch ingestion."""
    from scans.models import ScanBatch

    normalized_fields = ScanBatch.normalize_batch_request_fields(
        r2_prefix=request.POST.get("r2_prefix", ""),
        payment_method=request.POST.get("payment_method", ""),
        scan_form_type=request.POST.get("scan_form_type", ""),
    )
    return {
        "r2_prefix": str(normalized_fields["r2_prefix"]),
        "appeal_code": request.POST.get("appeal_code", "").strip(),
        "payment_method": str(normalized_fields["payment_method"]),
        "scan_form_type": str(normalized_fields["scan_form_type"]),
    }


def _validate_new_batch_post(post_data: dict[str, str]) -> str | None:
    """Validate required fields and layout compatibility for new ingestion."""
    from scans.models import ScanBatch

    return ScanBatch.batch_request_error(
        post_data["payment_method"],
        post_data["scan_form_type"],
        missing_payment_method_message="Missing required fields.",
        missing_scan_form_type_message="Missing required fields.",
        invalid_payment_method_message="Invalid form layout for the selected payment method.",
        invalid_layout_message="Invalid form layout for the selected payment method.",
    )


def _serialize_created_batches(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach detail URLs for rendered HTMX ingest responses."""
    serialized_batches: list[dict[str, Any]] = []
    for batch in batches:
        serialized_batches.append(
            {
                "batch_name": batch["batch_name"],
                "file_count": batch["file_count"],
                "detail_url": reverse(
                    "custom_admin:scan_batch_detail_view",
                    kwargs={"scan_batch_id": batch["scan_batch_id"]},
                ),
            }
        )
    return serialized_batches


def _render_ingest_result(result: dict[str, Any], scan_form_type: str) -> HttpResponse:
    """Render the HTMX badge for a completed folder-ingest request."""
    result_status = str(result.get("status") or "error")
    context = {
        "result_status": result_status,
        "scan_form_type": scan_form_type,
        "message": str(result.get("error") or result.get("message") or ""),
        "file_count": int(result.get("file_count") or 0),
        "total_batches": int(result.get("total_batches") or 0),
        "batches": _serialize_created_batches(
            result.get("batches", []) if isinstance(result.get("batches"), list) else []
        ),
    }
    html = render_to_string(
        "admin/scan_processing/partials/ingest_result_badge.html",
        context,
    )
    status = 200 if result_status == "ok" else 400
    return HttpResponse(html, status=status)


def _handle_new_batch_post(request: HttpRequest) -> HttpResponse:
    """Handle HTMX ingestion of a single pending scan folder."""
    from scans.scan_folder import ScanFolderWatcherService

    post_data = _get_new_batch_post_data(request)
    validation_error = _validate_new_batch_post(post_data)
    if validation_error:
        return _error_badge(validation_error)

    result = ScanFolderWatcherService.ingest_folder(
        appeal_code=post_data["appeal_code"],
        payment_method=post_data["payment_method"],
        scan_form_type=post_data["scan_form_type"],
        r2_prefix=post_data["r2_prefix"],
        user=request.user,
        auto_process=True,
    )
    return _render_ingest_result(result, post_data["scan_form_type"])


@is_authenticated_and_is_staff
def scan_processing_dashboard(request: HttpRequest) -> HttpResponse:
    """Scan processing dashboard showing all scan batches.

    URL: /admin/scan-processing/

    Displays scan batches grouped by campaign with status indicators,
    OCR progress, and links to review extracted data.

    Args:
        request: HTTP request.

    Returns:
        HttpResponse: Rendered dashboard page.
    """
    campaign_id = request.GET.get("campaign_id", "")
    attention_filter = request.GET.get("attention", "")
    status_filter = request.GET.get("status", "")
    if status_filter not in ALLOWED_STATUS_FILTERS:
        status_filter = "active"
    try:
        page = max(int(request.GET.get("page") or 1), 1)
    except TypeError, ValueError:
        page = 1
    try:
        per_page = int(request.GET.get("per_page") or DEFAULT_DASHBOARD_PER_PAGE)
    except TypeError, ValueError:
        per_page = DEFAULT_DASHBOARD_PER_PAGE
    per_page = max(min(per_page, 200), 1)
    context = _get_dashboard_context(
        campaign_id, attention_filter, status_filter, page, per_page
    )
    return render(request, "admin/scan_processing/dashboard.html", context)


@is_authenticated_and_is_staff
@require_POST
def scan_redacted_pages_upload(
    request: HttpRequest, scan_batch_id: str
) -> HttpResponse:
    """Accept multipart redacted page uploads and replace placeholder R2 keys."""
    from core.storage_backends import r2_enabled
    from scans.models import ScanPlaceholder
    from scans.scan_redaction import (
        expected_redaction_page_count,
        replace_placeholder_with_redacted_uploads,
    )

    if not r2_enabled():
        messages.error(request, "R2 storage is not configured.")
        return redirect(
            "custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id
        )

    placeholder_id = request.POST.get("placeholder_id", "").strip()
    if not placeholder_id:
        messages.error(request, "Missing placeholder.")
        return redirect(
            "custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id
        )

    ph = get_object_or_404(
        ScanPlaceholder.objects.select_related("batch"),
        id=placeholder_id,
        batch_id=scan_batch_id,
    )

    expected = expected_redaction_page_count(ph)
    if expected < 1:
        messages.error(
            request, "This placeholder has no recorded source pages to replace."
        )
        return redirect(
            "custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id
        )

    files = request.FILES.getlist("redacted_files")
    try:
        replace_placeholder_with_redacted_uploads(
            ph,
            files,
            user=cast(
                User | None,
                request.user if request.user.is_authenticated else None,
            ),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(
            "custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id
        )
    except Exception:
        logger.exception("Redacted upload failed for placeholder %s", ph.id)
        messages.error(
            request,
            "Upload failed (see server logs). Objects may be partially written.",
        )
        return redirect(
            "custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id
        )

    messages.success(request, "Redacted pages uploaded and saved successfully.")
    return redirect("custom_admin:scan_batch_detail_view", scan_batch_id=scan_batch_id)


@is_authenticated_and_is_staff
def scan_batch_detail_view(request: HttpRequest, scan_batch_id: str) -> HttpResponse:
    """Detail view for a single scan batch showing all placeholders.

    URL: /admin/scan-processing/<scan_batch_id>/

    Shows each scanned form with its OCR extraction results,
    donor matching status, and links to the created donation.

    Args:
        request: HTTP request.
        scan_batch_id: UUID of the ScanBatch.

    Returns:
        HttpResponse: Rendered detail page.
    """
    context = _get_scan_batch_detail_context(scan_batch_id)
    return render(request, "admin/scan_processing/batch_detail.html", context)


@is_authenticated_and_is_staff
def scan_new_batch_view(request: HttpRequest) -> HttpResponse:
    """List pending R2 intake folders and allow manual ingest.

    URL: /admin/scan-processing/new-batch/

    GET: Discovers R2 folders under the intake prefix that have not yet
    been ingested (no ``.done`` marker). Resolves each folder's
    ``appeal_code`` to a Campaign for display.

    POST (HTMX): Ingests one folder identified by ``r2_prefix``.
    Returns a small HTML snippet so HTMX can swap the result inline.

    Args:
        request: HTTP request.

    Returns:
        HttpResponse: Rendered page or HTMX partial.
    """
    from scans.scan_folder import ScanFolderWatcherService

    if request.method == "POST":
        return _handle_new_batch_post(request)

    # GET — discover pending folders
    pending_folders = ScanFolderWatcherService.list_pending_folders()

    context = {
        "active": "scan_processing",
        "pending_folders": pending_folders,
        "page_title": "Pending R2 Folders",
    }
    return render(request, "admin/scan_processing/new_batch.html", context)
