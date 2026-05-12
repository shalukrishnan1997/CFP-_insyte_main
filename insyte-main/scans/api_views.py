"""API views for scan processing and OCR management.

Provides endpoints for:
- Triggering scan batch OCR processing.
- Checking scan batch processing status.
- Listing scan batches and placeholders.
- Retrying failed scans (with rate limiting and audit logging).

Security notes:
- All endpoints require staff authentication (is_authenticated_and_is_staff).
- CSRF is enforced via @require_POST and the hx-headers X-CSRFToken sent by HTMX.
- Retry endpoints enforce a 60-second cooldown and a 3-per-10-minute limit per
  (user_id, resource_id) pair using Django's cache framework.
- All retry attempts are logged with user ID, resource ID and timestamp for audit.
"""

import logging
import mimetypes
from typing import Any

from django.core import signing
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET, require_POST

from responsehandling.permissions import is_authenticated_and_is_staff

logger = logging.getLogger(__name__)

# ───────────────────────── Rate-limit constants ──────────────────────────
_RETRY_COOLDOWN_SECS = 60  # minimum gap between two retries
_RETRY_WINDOW_SECS = 10 * 60  # sliding window for max-hits check
_RETRY_MAX_PER_WINDOW = 3  # max retries per window per (user, resource)
_PDF_PAGE_KEY_SEPARATOR = "::pdf_page::"
_SCAN_VIEW_TOKEN_SALT = "custom_admin.scan_view"
_SCAN_VIEW_TOKEN_MAX_AGE_SECS = 60 * 60
# ─────────────────────────────────────────────────────────────────────────


def build_scan_view_token(resource_id: str) -> str:
    """Return a signed short-lived token for scan viewer access.

    Args:
        resource_id: Placeholder UUID string.

    Returns:
        Signed token safe for query-string transport.
    """
    return signing.dumps({"id": resource_id}, salt=_SCAN_VIEW_TOKEN_SALT)


def _get_signed_scan_resource_id(request: HttpRequest) -> str | None:
    """Return the placeholder id from a valid signed token, if present.

    Args:
        request: Incoming request.

    Returns:
        Placeholder UUID string or ``None`` when the token is missing/invalid.
    """
    token = request.GET.get("token", "").strip()
    if not token:
        return None

    try:
        payload = signing.loads(
            token,
            salt=_SCAN_VIEW_TOKEN_SALT,
            max_age=_SCAN_VIEW_TOKEN_MAX_AGE_SECS,
        )
    except signing.BadSignature:
        logger.warning("Invalid signed scan viewer token")
        return None

    resource_id = str(payload.get("id", "") or "").strip()
    return resource_id or None


def _request_can_view_scan(request: HttpRequest, resource_id: str) -> bool:
    """Return whether the request is authorised to access a scan payload.

    Access is granted to staff-authenticated sessions or to requests bearing a
    valid signed scan-view token generated from the QA page.

    Args:
        request: Incoming request.
        resource_id: Placeholder UUID string.

    Returns:
        ``True`` when the request may access the scan.
    """
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        has_access = (
            getattr(user, "is_staff", False)
            or user.groups.exists()
            or user.user_permissions.exists()
        )
        if has_access:
            return True

    return _get_signed_scan_resource_id(request) == resource_id


def _parse_virtual_pdf_page_key(key: str) -> tuple[str, int | None]:
    """Parse a virtual PDF page key into source key and page number.

    Args:
        key: Real R2 object key or virtual key with page suffix.

    Returns:
        Tuple of (source_key, page_number or None).
    """
    if _PDF_PAGE_KEY_SEPARATOR not in key:
        return key, None

    source_key, _, page_text = key.rpartition(_PDF_PAGE_KEY_SEPARATOR)
    try:
        page_number = int(page_text)
    except ValueError:
        logger.warning("Invalid virtual PDF page key in image serve: %s", key)
        return key, None

    if page_number < 1:
        logger.warning("Invalid virtual PDF page number in image serve: %s", key)
        return key, None

    return source_key, page_number


def _build_placeholder_page_urls(
    placeholder: Any,
    user: Any | None = None,
    *,
    allow_pending_redaction: bool = False,
) -> list[str]:
    """Return signed image proxy URLs for the placeholder's stored pages."""
    from scans.donation_scan import DonationScanService

    return DonationScanService.get_placeholder_page_urls(
        placeholder,
        user=user,
        allow_pending_redaction=allow_pending_redaction,
    )


def _check_retry_rate_limit(
    user_id: int | str,
    resource_id: str,
    resource_type: str = "batch",
) -> JsonResponse | None:
    """Enforce rate limits for scan retry endpoints.

    Uses two cache keys per (user, resource):
    1. A **cooldown** key that expires after ``_RETRY_COOLDOWN_SECS``.
       If this key exists, the user tried too recently.
    2. A **hit-counter** key that expires after ``_RETRY_WINDOW_SECS``.
       If the counter exceeds ``_RETRY_MAX_PER_WINDOW``, the window is full.

    Args:
        user_id: ID of the authenticated user.
        resource_id: UUID of the batch or placeholder being retried.
        resource_type: Label used in log messages ("batch" or "placeholder").

    Returns:
        A 429 JsonResponse if rate limited, otherwise None.
    """
    cooldown_key = f"scan_retry_cooldown:{user_id}:{resource_id}"
    window_key = f"scan_retry_window:{user_id}:{resource_id}"

    # Check 60-second cooldown
    if cache.get(cooldown_key):
        logger.warning(
            "Scan retry rate-limited (cooldown): user=%s %s=%s",
            user_id,
            resource_type,
            resource_id,
        )
        return JsonResponse(
            {
                "error": "Please wait at least 60 seconds before retrying again.",
                "rate_limited": True,
                "retry_after": _RETRY_COOLDOWN_SECS,
            },
            status=429,
        )

    # Check per-window cap
    hit_count: int = cache.get(window_key, 0)
    if hit_count >= _RETRY_MAX_PER_WINDOW:
        logger.warning(
            "Scan retry rate-limited (window cap %d/%d): user=%s %s=%s",
            hit_count,
            _RETRY_MAX_PER_WINDOW,
            user_id,
            resource_type,
            resource_id,
        )
        return JsonResponse(
            {
                "error": (
                    f"Too many retries. You may retry at most {_RETRY_MAX_PER_WINDOW}"
                    f" times per {_RETRY_WINDOW_SECS // 60} minutes."
                ),
                "rate_limited": True,
                "retry_after": _RETRY_WINDOW_SECS,
            },
            status=429,
        )

    # Record this attempt
    cache.set(cooldown_key, True, timeout=_RETRY_COOLDOWN_SECS)
    cache.set(window_key, hit_count + 1, timeout=_RETRY_WINDOW_SECS)

    logger.info(
        "Scan retry allowed: user=%s %s=%s hit=%d/%d",
        user_id,
        resource_type,
        resource_id,
        hit_count + 1,
        _RETRY_MAX_PER_WINDOW,
    )
    return None


@is_authenticated_and_is_staff
@require_POST
def scan_batch_create(request: HttpRequest) -> JsonResponse:
    """Create a scan batch from R2 files and trigger OCR processing.

    POST /admin/api/scan-processing/create/

    JSON body:
        campaign_id (str): UUID of the campaign.
        r2_prefix (str): R2 key prefix to scan for images.
        payment_method (str): Payment method for all scans in this batch.
        scan_form_type (str): Physical document layout for this batch.
        batch_name (str, optional): Batch identifier matching the uploaded PDF filename.
        auto_process (bool, optional): Auto-trigger OCR (default: True).

    Returns:
        JsonResponse with scan_batch_id and status.
    """
    import json

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):  # fmt: skip
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    campaign_id = data.get("campaign_id", "")

    from scans.models import ScanBatch

    normalized_fields = ScanBatch.normalize_batch_request_fields(
        r2_prefix=data.get("r2_prefix", ""),
        payment_method=data.get("payment_method", ""),
        scan_form_type=data.get("scan_form_type", ""),
        batch_name=data.get("batch_name", ""),
        auto_process=data.get("auto_process", True),
    )
    r2_prefix = str(normalized_fields["r2_prefix"])
    payment_method = str(normalized_fields["payment_method"])
    scan_form_type = str(normalized_fields["scan_form_type"])
    batch_name = str(normalized_fields["batch_name"])
    auto_process = bool(normalized_fields["auto_process"])

    if not campaign_id:
        return JsonResponse({"error": "campaign_id is required"}, status=400)
    if not r2_prefix:
        return JsonResponse({"error": "r2_prefix is required"}, status=400)

    validation_error = ScanBatch.batch_request_error(
        payment_method,
        scan_form_type,
    )
    if validation_error is not None:
        return JsonResponse({"error": validation_error}, status=400)

    from campaigns.ingest_guards import campaign_scan_block_reason
    from campaigns.models import Campaign

    try:
        campaign = Campaign.objects.select_related("client").get(id=campaign_id)
    except Campaign.DoesNotExist:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    block_reason = campaign_scan_block_reason(campaign)
    if block_reason is not None:
        return JsonResponse({"error": block_reason}, status=400)

    from scans.tasks import create_scan_batch_from_r2_task

    task = create_scan_batch_from_r2_task.delay(
        campaign_id=str(campaign.id),
        r2_prefix=r2_prefix,
        payment_method=payment_method,
        scan_form_type=scan_form_type,
        batch_name=batch_name,
        user_id=request.user.id if request.user.is_authenticated else None,
        auto_process=auto_process,
    )

    return JsonResponse(
        {
            "success": True,
            "task_id": task.id,
            "message": "Scan batch creation started.",
        }
    )


@is_authenticated_and_is_staff
@require_POST
def scan_batch_process(request: HttpRequest) -> JsonResponse:
    """Trigger OCR processing for an existing scan batch.

    POST /admin/api/scan-processing/<scan_batch_id>/process/

    Returns:
        JsonResponse with task_id.
    """
    import json

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
            scan_batch_id = data.get("scan_batch_id", "")
        except json.JSONDecodeError, ValueError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
    else:
        scan_batch_id = request.POST.get("scan_batch_id", "")

    if not scan_batch_id:
        return JsonResponse({"error": "scan_batch_id is required"}, status=400)

    from scans.models import ScanBatch

    try:
        scan_batch = ScanBatch.objects.get(id=scan_batch_id)
    except ScanBatch.DoesNotExist:
        return JsonResponse({"error": "Scan batch not found"}, status=404)

    if scan_batch.status == ScanBatch.STATUS_COMPLETED:
        return JsonResponse(
            {"error": "Scan batch has already been processed successfully"},
            status=400,
        )

    from scans.tasks import process_scan_batch_task

    task = process_scan_batch_task.delay(str(scan_batch_id))

    return JsonResponse(
        {
            "success": True,
            "task_id": task.id,
            "message": "Scan batch processing started.",
        }
    )


@is_authenticated_and_is_staff
@require_GET
def scan_batch_status(request: HttpRequest) -> JsonResponse:
    """Get processing status for a scan batch.

    GET /admin/api/scan-processing/status/?scan_batch_id=<uuid>

    Returns:
        JsonResponse with detailed status including placeholder counts.
    """
    scan_batch_id = request.GET.get("scan_batch_id", "")
    if not scan_batch_id:
        return JsonResponse({"error": "scan_batch_id is required"}, status=400)

    from scans.scan_processing import ScanProcessingService

    result = ScanProcessingService.get_scan_batch_status(scan_batch_id)
    if "error" in result:
        return JsonResponse(result, status=404)

    return JsonResponse(result)


@is_authenticated_and_is_staff
@require_GET
def scan_batch_list(request: HttpRequest) -> JsonResponse:
    """List scan batches for a campaign.

    GET /admin/api/scan-processing/list/?campaign_id=<uuid>

    Returns:
        JsonResponse with list of scan batches.
    """
    campaign_id = request.GET.get("campaign_id", "")
    if not campaign_id:
        return JsonResponse({"error": "campaign_id is required"}, status=400)

    from scans.models import ScanBatch

    batches = (
        ScanBatch.objects.filter(campaign_id=campaign_id)
        .select_related("donation_batch", "created_by")
        .order_by("-created_at")[:20]
    )

    batch_list: list[dict[str, Any]] = []
    for batch in batches:
        batch_list.append(
            {
                "id": str(batch.id),
                "batch_name": batch.batch_name,
                "payment_method": batch.payment_method,
                "status": batch.status,
                "total_scans": batch.total_scans,
                "processed_scans": batch.processed_scans,
                "matched_scans": batch.matched_scans,
                "progress_pct": batch.progress_pct,
                "donation_batch_id": (
                    batch.donation_batch_id if batch.donation_batch_id else None
                ),
                "created_at": batch.created_at.isoformat(),
                "created_by": (
                    batch.created_by.get_full_name() if batch.created_by else ""
                ),
            }
        )

    return JsonResponse({"batches": batch_list})


@is_authenticated_and_is_staff
@require_GET
def scan_placeholder_list(request: HttpRequest) -> JsonResponse:
    """List scan placeholders for a scan batch.

    GET /admin/api/scan-processing/placeholders/?scan_batch_id=<uuid>

    Returns:
        JsonResponse with list of placeholders and their OCR results.
    """
    scan_batch_id = request.GET.get("scan_batch_id", "")
    if not scan_batch_id:
        return JsonResponse({"error": "scan_batch_id is required"}, status=400)

    from scans.models import ScanPlaceholder

    placeholders = (
        ScanPlaceholder.objects.filter(batch_id=scan_batch_id)
        .select_related("matched_donor", "matched_data_file_donor", "donation")
        .order_by("created_at")[:100]
    )

    placeholder_list: list[dict[str, Any]] = []
    for ph in placeholders:
        extracted = ph.extracted_data or {}
        page_urls = _build_placeholder_page_urls(ph)
        image_proxy_url = page_urls[0] if page_urls else ""
        placeholder_list.append(
            {
                "id": str(ph.id),
                "image_url": image_proxy_url,
                "page_urls": page_urls,
                "urn": ph.urn,
                "donor_name": ph.donor_name,
                "ocr_status": ph.ocr_status,
                "ocr_confidence": ph.ocr_confidence,
                "is_captured": ph.is_captured,
                "is_matched": ph.is_matched,
                "extracted_amount": extracted.get("amount", ""),
                "extracted_gift_aid": extracted.get("gift_aid"),
                "extracted_payment_method": extracted.get("payment_method", ""),
                "extracted_date": extracted.get("donation_date", ""),
                "processing_error": ph.processing_error[:200]
                if ph.processing_error
                else "",
                "donation_id": str(ph.donation_id) if ph.donation_id else None,
            }
        )

    return JsonResponse({"placeholders": placeholder_list})


@require_GET
@xframe_options_exempt
def scan_placeholder_view(request: HttpRequest) -> HttpResponse:
    """Render a single-placeholder viewer page using canonical page images."""
    import json

    from django.shortcuts import render

    placeholder_id = request.GET.get("id", "").strip()
    if not placeholder_id:
        return JsonResponse({"error": "id is required"}, status=400)

    if not _request_can_view_scan(request, placeholder_id):
        return JsonResponse({"error": "Authentication required"}, status=403)

    try:
        from scans.models import ScanPlaceholder

        placeholder = ScanPlaceholder.objects.select_related(
            "batch",
            "matched_donor",
            "matched_data_file_donor",
        ).get(id=placeholder_id)
    except Exception:
        return JsonResponse({"error": "Scan not found"}, status=404)

    page_urls = _build_placeholder_page_urls(
        placeholder,
        user=request.user if getattr(request.user, "is_authenticated", False) else None,
        allow_pending_redaction=True,
    )
    subtitle_parts: list[str] = []
    if placeholder.urn:
        subtitle_parts.append(f"URN {placeholder.urn}")
    if placeholder.batch and placeholder.batch.batch_name:
        subtitle_parts.append(placeholder.batch.batch_name)

    return render(
        request,
        "admin/scan_processing/placeholder_view.html",
        {
            "placeholder": placeholder,
            "page_urls_json": json.dumps(page_urls),
            "image_url": page_urls[0] if page_urls else "",
            "has_image": bool(page_urls),
            "subtitle": " • ".join(subtitle_parts),
        },
    )


@is_authenticated_and_is_staff
@require_POST
def scan_retry_failed(request: HttpRequest) -> JsonResponse:
    """Retry all failed scans in a batch.

    POST /admin/api/scan-processing/retry/

    JSON body:
        scan_batch_id (str): UUID of the ScanBatch.

    Returns:
        JsonResponse with task_id.
    """
    import json

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
            scan_batch_id = data.get("scan_batch_id", "")
        except json.JSONDecodeError, ValueError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
    else:
        scan_batch_id = request.POST.get("scan_batch_id", "")

    if not scan_batch_id:
        return JsonResponse({"error": "scan_batch_id is required"}, status=400)

    user_id: int | str = (
        (request.user.pk or "anon") if request.user.is_authenticated else "anon"
    )
    rate_limit_resp = _check_retry_rate_limit(user_id, scan_batch_id, "batch")
    if rate_limit_resp:
        return rate_limit_resp

    from scans.tasks import retry_failed_scans_task

    task = retry_failed_scans_task.delay(str(scan_batch_id))

    return JsonResponse(
        {
            "success": True,
            "task_id": task.id,
            "message": "Retry started for failed scans.",
        }
    )


@is_authenticated_and_is_staff
@require_POST
def scan_retry_single(request: HttpRequest) -> JsonResponse:
    """Retry a single scan placeholder.

    POST /admin/api/scan-processing/retry-single/

    JSON body:
        placeholder_id (str): UUID of the ScanPlaceholder.

    Returns:
        JsonResponse with task_id.
    """
    import json

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
            placeholder_id = data.get("placeholder_id", "")
        except json.JSONDecodeError, ValueError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
    else:
        placeholder_id = request.POST.get("placeholder_id", "")

    if not placeholder_id:
        return JsonResponse({"error": "placeholder_id is required"}, status=400)

    user_id: int | str = (
        (request.user.pk or "anon") if request.user.is_authenticated else "anon"
    )
    rate_limit_resp = _check_retry_rate_limit(user_id, placeholder_id, "placeholder")
    if rate_limit_resp:
        return rate_limit_resp

    from scans.tasks import process_single_scan_task

    task = process_single_scan_task.delay(str(placeholder_id))

    return JsonResponse(
        {
            "success": True,
            "task_id": task.id,
            "message": "Retry started for scan.",
        }
    )


@require_GET
@xframe_options_exempt
def scan_placeholder_pdf(request: HttpRequest) -> HttpResponse:
    """Serve all pages for a single donor as one merged PDF.

    Builds a merged PDF from the placeholder's stored page objects. Page keys
    may reference either single-page PDFs or raster images produced during PDF
    splitting. For single-page documents the result is a one-page PDF; for
    duplex / *_with_payment documents all donor pages are concatenated in order.

    GET /admin/api/scan-processing/placeholder-pdf/?id=<placeholder_uuid>

    Args:
        request: HTTP request.

    Returns:
        Merged single-donor PDF response, or error JSON.
    """
    from core.storage_backends import r2_enabled
    from scans.scan_processing_r2 import build_pdf_bytes_from_r2_keys

    placeholder_id = request.GET.get("id", "").strip()
    if not placeholder_id:
        return JsonResponse({"error": "id is required"}, status=400)

    if not _request_can_view_scan(request, placeholder_id):
        return JsonResponse({"error": "Authentication required"}, status=403)

    if not r2_enabled():
        return JsonResponse({"error": "R2 storage not configured"}, status=503)

    try:
        from scans.models import ScanPlaceholder

        placeholder = ScanPlaceholder.objects.select_related("batch").get(
            id=placeholder_id
        )
    except Exception:
        return JsonResponse({"error": "Scan not found"}, status=404)

    page_keys: list[str] = placeholder.page_keys or []
    if not page_keys:
        # Single-page document — treat image_path as the sole key
        page_keys = [placeholder.image_path]

    # PCI audit: if the placeholder still has unredacted bytes, log who
    # built the merged PDF so auditors have a trail. Once redaction is
    # COMPLETED the merged PDF is just sanitized output.
    from scans.models import ScanPlaceholder as _SP

    is_unredacted = placeholder.redaction_status != _SP.REDACTION_COMPLETED
    if is_unredacted:
        try:
            from audit.utils import log_request_action

            log_request_action(
                request,
                action="VIEW",
                model_name="ScanPlaceholder",
                object_id=str(placeholder.id),
                object_repr=str(placeholder),
                summary="Built unredacted donor PDF (merged scan)",
                changes={
                    "redaction_status": placeholder.redaction_status,
                    "page_count": len(page_keys),
                },
            )
        except Exception:
            logger.exception(
                "Failed to write unredacted-PDF AuditLog for placeholder %s",
                placeholder.id,
            )

    try:
        merged_pdf = build_pdf_bytes_from_r2_keys(page_keys)

        urn = placeholder.urn or str(placeholder.id)[:8]
        safe_filename = f"scan_{urn}.pdf"

        cache_control = "no-store" if is_unredacted else "private, max-age=3600"

        return HttpResponse(
            merged_pdf,
            content_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{safe_filename}"',
                "Cache-Control": cache_control,
            },
        )
    except Exception:
        logger.exception(
            "Failed to build donor PDF for placeholder '%s'", placeholder_id
        )
        return JsonResponse({"error": "Could not generate PDF"}, status=500)


@require_GET
@xframe_options_exempt
def scan_image_serve(request: HttpRequest) -> HttpResponse:
    """Serve a scan image or a specific page extracted from a PDF.

    The R2 bucket is private (not publicly accessible). This endpoint
    authenticates the Django staff user then either:
    - Extracts a specific page from a multi-donor PDF and returns it
      as a single-page PDF response (for virtual page keys).
    - Redirects to a presigned URL for non-paginated keys (images, etc.).

    GET /admin/api/scan-processing/image/?key=<r2_object_key>

    Supports both real R2 keys and virtual PDF page keys in the format:
    ``<source-key>::pdf_page::<page_number>``.

    Args:
        request: HTTP request.

    Returns:
        Single-page PDF response (for virtual keys), 302 redirect for plain
        keys, or error JSON if R2 is not configured / key is missing.
    """
    import io

    from django.conf import settings

    from core.storage_backends import get_r2_client, r2_enabled, r2_presigned_url

    key = request.GET.get("key", "").strip()
    if not key:
        return JsonResponse({"error": "key is required"}, status=400)
    allow_pending_redaction = request.GET.get("allow_pending_redaction") == "1"
    inline_proxy = request.GET.get("inline") == "1"

    placeholder_id = request.GET.get("id", "").strip()
    # Always enforce auth — do not skip when 'id' is absent.
    if not _request_can_view_scan(request, placeholder_id):
        return JsonResponse({"error": "Authentication required"}, status=403)

    from scans.models import ScanPlaceholder
    from scans.scan_redaction import (
        can_view_unredacted_pending_scan,
        manual_redaction_required_for,
        placeholder_payment_method,
        placeholder_request_key_allowed,
    )

    ph: ScanPlaceholder | None = None
    if placeholder_id:
        try:
            ph = ScanPlaceholder.objects.get(id=placeholder_id)
        except ScanPlaceholder.DoesNotExist, ValueError:
            return JsonResponse({"error": "Unknown placeholder"}, status=404)

    if ph is not None and ph.redaction_status != ScanPlaceholder.REDACTION_COMPLETED:
        if not placeholder_request_key_allowed(ph, key):
            return JsonResponse({"error": "Key does not match placeholder"}, status=403)
        payment_method = placeholder_payment_method(ph)
        if manual_redaction_required_for(payment_method):
            user = getattr(request, "user", None)
            if not can_view_unredacted_pending_scan(
                user, allow_qa_pending_redaction=allow_pending_redaction
            ):
                return JsonResponse(
                    {
                        "error": (
                            "Manual redaction must be completed before viewing "
                            "this scan"
                        )
                    },
                    status=403,
                )

    is_unredacted = (
        ph is not None and ph.redaction_status != ScanPlaceholder.REDACTION_COMPLETED
    )
    cache_control = "no-store" if is_unredacted else "private, max-age=3600"

    # PCI audit trail: log every successful read of an unredacted scan so
    # auditors can reconstruct who viewed PAN/CVV bytes and when. Failures
    # to log must not block the read itself (audit DB blip ≠ access denial).
    if is_unredacted and ph is not None:
        try:
            from audit.utils import log_request_action

            log_request_action(
                request,
                action="VIEW",
                model_name="ScanPlaceholder",
                object_id=str(ph.id),
                object_repr=str(ph),
                summary="Viewed unredacted scan image",
                changes={
                    "key": key,
                    "redaction_status": ph.redaction_status,
                    "allow_pending_redaction": allow_pending_redaction,
                },
            )
        except Exception:
            logger.exception(
                "Failed to write unredacted-view AuditLog for placeholder %s",
                ph.id,
            )

    if not r2_enabled():
        return JsonResponse({"error": "R2 storage not configured"}, status=503)

    source_key, page_number = _parse_virtual_pdf_page_key(key)

    if page_number is not None:
        # Extract only the requested page from the full multi-donor PDF so the
        # viewer never shows other donors' forms.
        try:
            from pypdf import PdfReader, PdfWriter

            client = get_r2_client()
            response = client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=source_key)
            pdf_bytes = response["Body"].read()

            reader = PdfReader(io.BytesIO(pdf_bytes))
            page_index = page_number - 1  # 1-based → 0-based

            if page_index < 0 or page_index >= len(reader.pages):
                return JsonResponse(
                    {"error": f"Page {page_number} not found in PDF"}, status=404
                )

            writer = PdfWriter()
            writer.add_page(reader.pages[page_index])

            out_buf = io.BytesIO()
            writer.write(out_buf)
            out_buf.seek(0)

            filename = source_key.rsplit("/", 1)[-1]
            base = filename.rsplit(".", 1)[0] if "." in filename else filename
            safe_filename = f"{base}_p{page_number:04d}.pdf"

            return HttpResponse(
                out_buf.read(),
                content_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{safe_filename}"',
                    "Cache-Control": cache_control,
                },
            )
        except Exception:
            logger.exception(
                "Failed to extract page %d from PDF key '%s'", page_number, source_key
            )
            # Fall back to presigned redirect if extraction fails
            presigned = r2_presigned_url(source_key, expiry=3600)
            if not presigned:
                return JsonResponse({"error": "Could not generate URL"}, status=404)
            redirect = HttpResponseRedirect(f"{presigned}#page={page_number}")
            if is_unredacted:
                redirect["Cache-Control"] = "no-store"
            return redirect

    if inline_proxy:
        try:
            client = get_r2_client()
            response = client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=source_key)
            body = response["Body"].read()
            content_type = (
                response.get("ContentType") or mimetypes.guess_type(source_key)[0]
            )
            return HttpResponse(
                body,
                content_type=content_type or "application/octet-stream",
                headers={"Cache-Control": cache_control},
            )
        except Exception:
            logger.exception("Failed to inline proxy image key '%s'", source_key)
            return JsonResponse({"error": "Could not read scan image"}, status=404)

    presigned = r2_presigned_url(source_key, expiry=3600)
    if not presigned:
        return JsonResponse({"error": "Could not generate URL"}, status=404)

    redirect = HttpResponseRedirect(presigned)
    if is_unredacted:
        redirect["Cache-Control"] = "no-store"
    return redirect
