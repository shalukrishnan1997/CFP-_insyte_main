"""Webhook handlers for Stripe payments and scanner upload notifications.

Handles:
    - Stripe payment webhooks with signature verification
    - Scanner upload webhooks for scanned form progress tracking
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import stripe
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

if TYPE_CHECKING:
    from scans.models import ScanUploadProgress

# from core.tasks import process_stripe_webhook moved inside functions

logger = logging.getLogger(__name__)

# NOTE: stripe.api_key is set per-call inside view functions, not at module level.
# This avoids loading the secret key at import time and ensures it's always current.

SCAN_COMPLETE_DEDUP_WINDOW = timedelta(hours=24)

# ─── Scan webhook replay / rate-limit constants ───
# Maximum allowed clock skew between scanner workstation and server (seconds).
# Requests outside this window are rejected as stale or future-dated.
SCAN_WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS = 60
# Replay-protection retention window. We remember (client_id, timestamp) for
# this many seconds and reject any duplicate within that window — this guards
# against replays that bypass the 24h batch-dedup hash (e.g. for "scanning"
# progress payloads, which don't dedup at all).
SCAN_WEBHOOK_REPLAY_WINDOW_SECONDS = 5 * 60
# Per-client rate limit: requests per minute.
SCAN_WEBHOOK_RATE_LIMIT_PER_MINUTE = 60
# Cache key prefixes (kept short to leave room under any KEY_PREFIX).
_SCAN_REPLAY_CACHE_PREFIX = "scan_wh:replay"
_SCAN_NONCE_CACHE_PREFIX = "scan_wh:nonce"
_SCAN_RATELIMIT_CACHE_PREFIX = "scan_wh:rl"


# Sentinel returned by ``_handle_upload_complete`` when the rejection should
# surface as HTTP 413 (Payload Too Large) instead of HTTP 400.
class _UploadCompletionError:
    """Tagged validation error from upload-completion processing."""

    def __init__(self, message: str, status: int = 400) -> None:
        self.message = message
        self.status = status


def _get_stripe_webhook_secrets() -> list[str]:
    """Return unique webhook signing secrets from active client Stripe configs."""
    from payments.models import PaymentGatewayConfig

    secrets: list[str] = []
    configs = PaymentGatewayConfig.objects.filter(
        provider="stripe",
        is_active=True,
    )

    for config in configs.iterator():
        secret = config.get_webhook_secret()
        if secret and secret not in secrets:
            secrets.append(secret)

    return secrets


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    """Stripe webhook endpoint.

    URL: /api/webhooks/stripe/

    Verifies webhook signature and queues event for async processing.
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    webhook_secrets = _get_stripe_webhook_secrets()

    if not webhook_secrets:
        # Fail-closed but loud: every Stripe-shaped POST is rejected with 500
        # until at least one PaymentGatewayConfig has a webhook secret set.
        # Use CRITICAL so the missing-config state pages ops, not just logs.
        logger.critical(
            "No active client PaymentGatewayConfig has a webhook_secret_encrypted; "
            "cannot verify Stripe webhook signatures."
        )
        return JsonResponse({"error": "Webhook secret not configured"}, status=500)

    # Verify webhook signature against any configured client secret.
    event = None
    for webhook_secret in webhook_secrets:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            break

        except ValueError:
            # Invalid payload
            logger.error("Invalid webhook payload")
            return JsonResponse({"error": "Invalid payload"}, status=400)

        except stripe.SignatureVerificationError:
            continue

    if event is None:
        logger.error("Invalid webhook signature")
        return JsonResponse({"error": "Invalid signature"}, status=400)

    try:
        # Store webhook event (idempotent — skip if already received)
        from payments.models import StripeWebhookEvent

        # Check if this event was already received (idempotency guard)
        if StripeWebhookEvent.objects.filter(stripe_event_id=event.id).exists():
            logger.info("Duplicate webhook event %s — skipping", event.id)
            return HttpResponse(status=200)

        try:
            from django.conf import settings as django_settings

            from payments.stripe_payload_sanitize import (
                maybe_persist_webhook_payload,
            )

            event_dict = event.to_dict()  # pyright: ignore[reportDeprecated]
            store_full = getattr(django_settings, "STORE_STRIPE_RAW_PAYLOADS", True)
            payload_to_store = maybe_persist_webhook_payload(
                event_dict, store_full=store_full
            )
            webhook_event = StripeWebhookEvent.objects.create(
                stripe_event_id=event.id,
                event_type=event.type,
                payload=payload_to_store,
                processed=False,
            )
        except IntegrityError:
            # Race condition: another request created it between check and create
            logger.info("Duplicate webhook event %s (race) — skipping", event.id)
            return HttpResponse(status=200)

        # Queue for async processing
        from core.tasks import process_stripe_webhook

        process_stripe_webhook.delay(str(webhook_event.id))

        logger.info("Webhook event %s queued for processing", event.id)

        # Return 200 immediately to acknowledge receipt
        return HttpResponse(status=200)

    except Exception as e:
        logger.exception("Error storing webhook event: %s", e)
        # Return 500 so Stripe retries later — event was NOT stored
        return HttpResponse(status=500)


# ═══════════════════════════════════════════════════════════════════
# Scanner Upload Webhook
# ═══════════════════════════════════════════════════════════════════


def _verify_scan_signature(
    payload: bytes,
    signature: str,
    timestamp: str,
) -> bool:
    """Verify HMAC-SHA256 signature from the scanner sync script.

    The canonical signed bytes are ``timestamp.encode() + b"." + payload``.
    Signing the timestamp alongside the body binds the signature to a
    specific moment in time, so an attacker who captures a valid request
    can't replay it indefinitely (the server also enforces a
    ``SCAN_WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS`` window on the timestamp
    itself).

    Args:
        payload: Raw request body (or query string for GET endpoints).
        signature: Hex-encoded HMAC-SHA256 digest sent in ``X-Signature``.
        timestamp: ``X-Scan-Timestamp`` header value, prepended to the
            signed bytes (separated by a literal ``.``).
    """
    secret = getattr(settings, "SCAN_WEBHOOK_SECRET", "")
    if not secret:
        return False
    signed_bytes = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_scan_timestamp(raw: str | None) -> int | None:
    """Parse a scanner-supplied unix timestamp string into an int.

    Returns ``None`` when ``raw`` is missing, blank, or unparseable. Floats
    are accepted (truncated) to be lenient with shell/Python clients.
    """
    if not raw:
        return None
    try:
        return int(float(raw))
    except TypeError, ValueError:
        return None


def _scan_timestamp_within_skew(timestamp: int, *, now: int | None = None) -> bool:
    """Return True if ``timestamp`` is within the allowed clock skew of now."""
    current = int(now if now is not None else time.time())
    return abs(current - timestamp) <= SCAN_WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS


def _claim_scan_replay_slot(client_id: str, timestamp: int) -> bool:
    """Atomically reserve a ``(client_id, timestamp)`` tuple in the cache.

    Uses ``cache.add`` so two concurrent requests with the same tuple race
    cleanly: only the first call returns ``True`` and every later call within
    the TTL returns ``False``. Both django-redis (``SET NX EX``) and
    LocMemCache implement ``add`` atomically.

    Returns:
        ``True`` when the slot was newly claimed; ``False`` if the same tuple
        was seen within ``SCAN_WEBHOOK_REPLAY_WINDOW_SECONDS``.
    """
    key = f"{_SCAN_REPLAY_CACHE_PREFIX}:{client_id}:{timestamp}"
    return bool(cache.add(key, "1", SCAN_WEBHOOK_REPLAY_WINDOW_SECONDS))


def _claim_scan_nonce_slot(client_id: str, nonce: str) -> bool:
    """Atomically reserve a ``(client_id, nonce)`` tuple in the cache.

    Sister of :func:`_claim_scan_replay_slot`, but keyed on a caller-supplied
    nonce (UUID hex, random string, etc.) rather than on the wall-clock
    timestamp. Lets the scanner workstation send an explicit anti-replay
    token in the body — useful when two legitimate requests need to share a
    second-precision timestamp (e.g. burst progress updates) without one of
    them being incorrectly flagged as a replay.

    Returns:
        ``True`` when the slot was newly claimed; ``False`` if the same
        ``(client_id, nonce)`` pair was seen within
        ``SCAN_WEBHOOK_REPLAY_WINDOW_SECONDS``.
    """
    key = f"{_SCAN_NONCE_CACHE_PREFIX}:{client_id}:{nonce}"
    return bool(cache.add(key, "1", SCAN_WEBHOOK_REPLAY_WINDOW_SECONDS))


def _enforce_scan_rate_limit(client_id: str, *, now: int | None = None) -> bool:
    """Increment the per-client per-minute counter; return True when allowed.

    Implements a fixed-window minute bucket. ``cache.incr`` is atomic on both
    Redis (django-redis ``INCR``) and LocMemCache (lock-protected), so two
    concurrent requests can never double-count a slot. We seed the counter
    with ``cache.add`` first because ``incr`` raises ``ValueError`` on
    missing keys.

    Returns:
        ``True`` when the request is within budget; ``False`` when the
        client has exceeded ``SCAN_WEBHOOK_RATE_LIMIT_PER_MINUTE`` requests
        this minute.
    """
    bucket = int(now if now is not None else time.time()) // 60
    key = f"{_SCAN_RATELIMIT_CACHE_PREFIX}:{client_id}:{bucket}"
    # Seed with 0 (no-op if the key already exists). 70s TTL gives a 10s
    # cross-bucket grace period so a slow client at second 59 isn't unfairly
    # blocked by a stale value at second 0 of the next minute.
    cache.add(key, 0, 70)
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add() and incr(); treat as first request in
        # the new bucket.
        cache.set(key, 1, 70)
        count = 1
    return count <= SCAN_WEBHOOK_RATE_LIMIT_PER_MINUTE


def _scan_completion_request_hash(
    campaign_id: str,
    r2_prefix: str,
    payment_method: str,
    scan_form_type: str,
    batch_name: str,
) -> str:
    """Return a stable SHA-256 hash of the completion-relevant request fields.

    Note:
        This helper is intentionally only invoked from the complete-status branch
        of the scan-upload webhook. The hardcoded ``"status": "complete"`` member
        is part of the canonical payload so the hash never collides with any
        future non-complete dedup keys reusing the same field set.
    """
    canonical = json.dumps(
        {
            "campaign_id": str(campaign_id),
            "r2_prefix": r2_prefix,
            "payment_method": payment_method,
            "scan_form_type": scan_form_type,
            "batch_name": batch_name,
            "status": "complete",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_pdf_page_limits(r2_prefix: str) -> _UploadCompletionError | None:
    """Probe PDFs under ``r2_prefix`` and reject if any exceed the page cap.

    Walks the R2 prefix listed by :func:`scans.scan_folder._list_all_keys_under_prefix`,
    downloads each ``*.pdf`` object, and counts pages using :mod:`pypdf`. If
    any single PDF exceeds ``MAX_OCR_PAGES_PER_PDF`` (default 100), returns a
    tagged HTTP 413 error so the scanner workstation is told the upload is
    too large *before* the OCR Celery task is enqueued — saving Document AI
    quota and worker time.

    Failures to list R2 objects, fetch a key, or parse a PDF are logged at
    WARNING and treated as "no limit hit" so a flaky probe never blocks a
    legitimate upload — the in-process OCR call has its own page-count
    guard and will reject the same PDF later.

    Args:
        r2_prefix: R2 object key prefix to probe.

    Returns:
        :class:`_UploadCompletionError` with status 413 when an oversize PDF
        is found, otherwise ``None``.
    """
    limit = int(getattr(settings, "MAX_OCR_PAGES_PER_PDF", 100) or 100)
    if limit <= 0:
        return None

    try:
        from scans.scan_folder import _list_all_keys_under_prefix
        from scans.scan_processing_r2 import count_pdf_pages
    except ImportError:
        logger.warning(
            "PDF page-count probe unavailable; allowing upload through",
            exc_info=True,
        )
        return None

    try:
        keys = _list_all_keys_under_prefix(r2_prefix)
    except Exception:
        logger.warning(
            "Failed to list R2 prefix %s for PDF page-count probe",
            r2_prefix,
            exc_info=True,
        )
        return None

    pdf_keys = [k for k in keys if k.lower().endswith(".pdf")]
    if not pdf_keys:
        return None

    try:
        from core.storage_backends import get_r2_client, r2_enabled
    except ImportError:
        return None

    if not r2_enabled():
        return None

    r2 = get_r2_client()
    bucket = settings.R2_BUCKET_NAME

    for key in pdf_keys:
        try:
            obj = r2.get_object(Bucket=bucket, Key=key)
            pdf_bytes: bytes = obj["Body"].read()
        except Exception:
            logger.warning(
                "PDF page-count probe: failed to fetch %s from R2", key, exc_info=True
            )
            continue

        page_count = count_pdf_pages(pdf_bytes)
        if page_count is None:
            continue

        if page_count > limit:
            return _UploadCompletionError(
                f"PDF '{key}' has {page_count} pages, exceeds "
                f"MAX_OCR_PAGES_PER_PDF={limit}",
                status=413,
            )

    return None


def _handle_upload_complete(
    data: dict[str, Any],
    campaign_id: str,
    progress: ScanUploadProgress,
    response_data: dict[str, Any],
) -> _UploadCompletionError | None:
    """Trigger OCR processing when a scan upload is complete.

    Args:
        data: Webhook payload dict.
        campaign_id: Campaign UUID string.
        progress: ``ScanUploadProgress`` row for the campaign — used to
            deduplicate replayed deliveries by comparing canonical request
            hashes against the most recent dispatch.
        response_data: Response dict to update with task info.

    Returns:
        :class:`_UploadCompletionError` when the completion payload is
        invalid (carries the HTTP status code to return), otherwise
        ``None``.
    """
    from scans.models import ScanBatch, ScanUploadProgress

    server_auto = getattr(settings, "OCR_AUTO_PROCESS_ON_UPLOAD", True)
    normalized_fields = ScanBatch.normalize_batch_request_fields(
        r2_prefix=data.get("r2_prefix", ""),
        payment_method=data.get("payment_method", ""),
        scan_form_type=data.get("scan_form_type", ""),
        batch_name=data.get("batch_name", ""),
        auto_process=data.get("auto_process", server_auto),
    )
    r2_prefix = str(normalized_fields["r2_prefix"])
    payment_method = str(normalized_fields["payment_method"])
    scan_form_type = str(normalized_fields["scan_form_type"])
    batch_name = str(normalized_fields["batch_name"])
    auto_process = bool(normalized_fields["auto_process"])

    if not auto_process:
        return None

    if not r2_prefix:
        return _UploadCompletionError("r2_prefix is required when status is complete")
    validation_error = ScanBatch.batch_request_error(
        payment_method,
        scan_form_type,
        missing_payment_method_message="payment_method is required when status is complete",
        missing_scan_form_type_message="scan_form_type is required when status is complete",
        invalid_payment_method_message="Invalid payment_method: {payment_method}",
        invalid_layout_message=(
            "Invalid scan_form_type '{scan_form_type}' for payment method "
            "'{payment_method}'. Allowed values: {allowed_labels}."
        ),
    )
    if validation_error is not None:
        return _UploadCompletionError(validation_error)

    page_limit_error = _check_pdf_page_limits(r2_prefix)
    if page_limit_error is not None:
        return page_limit_error

    # Pre-flight check for the (campaign, batch_name) unique constraint so
    # we surface a 409 before the Celery task swallows the IntegrityError in
    # the worker. Without this, a duplicate filename would either return 500
    # (raised at .delay() time on eager backends) or fail silently in the
    # worker with no operator-friendly response. Raising IntegrityError here
    # lets the request handler convert it into a 409 with a clear hint.
    if (
        batch_name
        and ScanBatch.objects.filter(
            campaign_id=campaign_id, batch_name=batch_name
        ).exists()
    ):
        logger.warning(
            "Scan upload completion: duplicate batch_name=%r for campaign %s",
            batch_name,
            campaign_id,
        )
        raise IntegrityError(
            f"ScanBatch already exists for campaign={campaign_id} "
            f"batch_name={batch_name!r}"
        )

    request_hash = _scan_completion_request_hash(
        campaign_id=str(campaign_id),
        r2_prefix=r2_prefix,
        payment_method=payment_method,
        scan_form_type=scan_form_type,
        batch_name=batch_name,
    )
    dispatched_at = timezone.now()
    cutoff = dispatched_at - SCAN_COMPLETE_DEDUP_WINDOW

    # Atomic claim: a single conditional UPDATE acts as the dedup guard. Two
    # concurrent webhook replays both racing to dispatch will hit the same row
    # and only one UPDATE will succeed (rowcount == 1); the loser's UPDATE
    # finds no rows matching the "not yet claimed" filter (rowcount == 0)
    # because the winner's UPDATE has already mutated the row in the same
    # transaction-isolation snapshot. This avoids the read-then-update race
    # where both requests pass an in-Python guard and both call .delay().
    with transaction.atomic():
        already_claimed = Q(
            last_completion_request_hash=request_hash,
            last_completion_dispatched_at__gte=cutoff,
        )
        rowcount = (
            ScanUploadProgress.objects.filter(pk=progress.pk)
            .exclude(already_claimed)
            .update(
                last_completion_request_hash=request_hash,
                last_completion_dispatched_at=dispatched_at,
            )
        )

    if rowcount == 0:
        # Another concurrent request already claimed this hash within the
        # dedup window. Re-read the row to surface the winner's task id.
        progress.refresh_from_db()
        response_data["status"] = "duplicate"
        response_data["ocr_task_id"] = progress.last_completion_task_id
        logger.info(
            "Duplicate scan-complete webhook for campaign %s — skipping enqueue (task=%s)",
            campaign_id,
            progress.last_completion_task_id,
        )
        return None

    from scans.tasks import create_scan_batch_from_r2_task

    task = create_scan_batch_from_r2_task.delay(
        campaign_id=str(campaign_id),
        r2_prefix=r2_prefix,
        payment_method=payment_method,
        scan_form_type=scan_form_type,
        batch_name=batch_name,
        user_id=None,
        auto_process=True,
    )
    # We've already claimed the slot above; record the dispatched task id.
    ScanUploadProgress.objects.filter(pk=progress.pk).update(
        last_completion_task_id=str(task.id),
    )
    response_data["ocr_task_id"] = task.id
    logger.info(
        "OCR processing triggered for campaign %s, prefix=%s",
        campaign_id,
        r2_prefix,
    )
    return None


@csrf_exempt
@require_POST
def scan_upload_webhook(request: HttpRequest) -> JsonResponse:
    """Webhook called by the scanner workstation sync script.

    URL: /webhooks/scan-upload/

    Canonical signed payload:
        The HMAC-SHA256 signature in ``X-Signature`` covers
        ``f"{timestamp}.{raw_body}"`` (timestamp prepended, separated by a
        literal ``.``). The timestamp must be supplied via the
        ``X-Scan-Timestamp`` header (or the ``timestamp`` body field as a
        fallback). Requests without a timestamp cannot produce a valid
        signature and are rejected with 403.

    Expected JSON body (progress):
    {
        "campaign_id": "<uuid>",
        "client_id": "<uuid>",
        "timestamp": 1714234567,        // unix seconds; can also be sent as
                                        // X-Scan-Timestamp header (header wins)
        "total_uploaded": 42,
        "total_expected": 100,
        "latest_urn": "IMG_0030.tiff",
        "status": "scanning",
        "payment_method": "cheque",
        "r2_prefix": "client/cheque/20260225-143022/",
        "batch_id": "20260225-143022"
    }

    Additional fields on completion (status="complete"):
        r2_prefix (required): R2 key prefix containing uploaded files.
        payment_method (required when status="complete"): Payment method for the
            batch.
        auto_process: Override server-side OCR_AUTO_PROCESS_ON_UPLOAD
                      setting (default: uses server setting).

    Validation rules:
        * Signature must be a valid HMAC-SHA256 over ``timestamp.body``.
        * ``abs(now - timestamp) <= 60s`` — else 401.
        * ``(client_id, timestamp)`` must not have been seen in the last
          5 minutes — else 401 (replay protection on top of the 24h batch
          dedup hash).
        * Per-client rate limit: 60 requests / minute → 429 when exceeded.
        * Duplicate ``(campaign, batch_name)`` → 409 with
          ``{"error": "duplicate_batch", ...}``.

    Headers:
        X-Signature: HMAC-SHA256 hex digest of ``f"{timestamp}.{body}"``.
        X-Scan-Timestamp: Unix timestamp (seconds) when the request was sent.
    """
    payload = request.body
    signature = request.META.get("HTTP_X_SIGNATURE", "")

    # Parse the body up-front so we can a) pull the timestamp out of it as a
    # fallback when no X-Scan-Timestamp header is sent, and b) reuse the
    # parsed dict below — without parsing twice. We tolerate bad JSON here
    # so the signature check can run first and reject unauthenticated noise
    # before we admit "Invalid JSON" details.
    try:
        data = json.loads(payload)
    except json.JSONDecodeError, ValueError:
        data = None

    raw_timestamp_header = request.META.get("HTTP_X_SCAN_TIMESTAMP")
    raw_timestamp_body: str | None = None
    raw_nonce_body: str | None = None
    if isinstance(data, dict):
        if not raw_timestamp_header and data.get("timestamp") is not None:
            raw_timestamp_body = str(data["timestamp"])
        nonce_value = data.get("nonce")
        if nonce_value is not None:
            raw_nonce_body = str(nonce_value)[:128]

    # Header wins over body so a stale captured request can't be cheaply
    # replayed by tweaking the body alone.
    timestamp_str = raw_timestamp_header or raw_timestamp_body
    timestamp_required = bool(
        getattr(settings, "SCAN_WEBHOOK_TIMESTAMP_REQUIRED", False)
    )

    # The signature must cover ``f"{timestamp}.{body}"``. Requests without
    # a timestamp cannot produce a valid signature and are rejected outright.
    sig_ok = (
        _verify_scan_signature(payload, signature, timestamp=timestamp_str)
        if timestamp_str is not None
        else False
    )
    if not sig_ok:
        logger.warning("Scan upload webhook: invalid signature")
        return JsonResponse({"error": "Invalid signature"}, status=403)

    parsed_timestamp = _parse_scan_timestamp(timestamp_str)
    if parsed_timestamp is None:
        if timestamp_required:
            logger.warning("Scan upload webhook: missing/invalid timestamp")
            return JsonResponse({"error": "missing or invalid timestamp"}, status=401)
    elif not _scan_timestamp_within_skew(parsed_timestamp):
        logger.warning(
            "Scan upload webhook: timestamp outside skew window (ts=%s)",
            parsed_timestamp,
        )
        return JsonResponse(
            {"error": "timestamp outside allowed skew window"}, status=401
        )

    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    campaign_id = data.get("campaign_id")
    client_id = data.get("client_id")
    if not campaign_id:
        return JsonResponse({"error": "campaign_id is required"}, status=400)
    if not client_id:
        return JsonResponse({"error": "client_id is required"}, status=400)

    # Per-client rate limit. Apply *before* hitting the DB so an abusive
    # client can't run up our query budget.
    if not _enforce_scan_rate_limit(str(client_id)):
        logger.warning(
            "Scan upload webhook: rate limit exceeded for client %s", client_id
        )
        return JsonResponse({"error": "rate limit exceeded"}, status=429)

    # Explicit nonce-based replay protection: when the scanner workstation
    # supplies a body-level ``nonce``, reject any second request that reuses
    # the same ``(client_id, nonce)`` within the replay window. This kicks in
    # *before* the timestamp window check and the hash-based completion
    # dedup so a captured-and-replayed request is rejected outright with
    # 401 (rather than silently deduped to 200/409).
    if raw_nonce_body is not None and not _claim_scan_nonce_slot(
        str(client_id), raw_nonce_body
    ):
        logger.warning(
            "Scan upload webhook: replay detected (client=%s, nonce=%s)",
            client_id,
            raw_nonce_body,
        )
        return JsonResponse({"error": "replayed nonce"}, status=401)

    # Replay protection: reject duplicate (client_id, timestamp) within the
    # 5-minute window. Skipped when no timestamp was supplied and timestamps
    # are not yet mandatory.
    if parsed_timestamp is not None and not _claim_scan_replay_slot(
        str(client_id), parsed_timestamp
    ):
        logger.warning(
            "Scan upload webhook: replay detected (client=%s, ts=%s)",
            client_id,
            parsed_timestamp,
        )
        return JsonResponse({"error": "replayed timestamp"}, status=401)

    from campaigns.models import Campaign
    from scans.models import ScanUploadProgress

    try:
        campaign = Campaign.objects.select_related("client").get(id=campaign_id)
        if str(campaign.client_id) != str(client_id):
            logger.warning(
                "Scan upload webhook: campaign/client mismatch "
                "campaign_id=%s client_id=%s",
                campaign_id,
                client_id,
            )
            return JsonResponse(
                {"error": "Campaign does not belong to client"}, status=403
            )
        from campaigns.ingest_guards import campaign_scan_block_reason

        block_reason = campaign_scan_block_reason(campaign)
        if block_reason is not None:
            return JsonResponse({"error": block_reason}, status=400)

        valid_statuses = {choice[0] for choice in ScanUploadProgress.STATUS_CHOICES}
        raw_status = str(data.get("status", "")).strip().lower()[:20]
        if raw_status not in valid_statuses:
            strict = getattr(settings, "STRICT_SCANNER_STATUS_VALIDATION", False)
            if strict:
                return JsonResponse(
                    {
                        "error": "invalid status",
                        "got": data.get("status"),
                        "allowed": sorted(valid_statuses),
                    },
                    status=400,
                )
            # Legacy behavior: log a WARNING with raw value + request fingerprint
            # then coerce to "scanning" so existing scanner workstations don't
            # break. Flip STRICT_SCANNER_STATUS_VALIDATION=True once all clients
            # are emitting valid status values.
            logger.warning(
                "Scan upload webhook: unknown status coerced to 'scanning' "
                "(raw=%r, campaign_id=%s, client_id=%s, remote=%s)",
                data.get("status"),
                campaign_id,
                client_id,
                request.META.get("REMOTE_ADDR", ""),
            )
            raw_status = ScanUploadProgress.STATUS_SCANNING

        # Cap error_message length consistently with other field truncations
        # (e.g. latest_urn[:255]) so a malicious or malformed scanner payload
        # can't blow up the row.
        last_error = (
            str(data.get("error_message", "")).strip()[:2000]
            if raw_status == ScanUploadProgress.STATUS_ERROR
            else ""
        )

        incoming_uploaded = int(data.get("total_uploaded", 0))
        incoming_expected = int(data.get("total_expected", 0))
        latest_urn = str(data.get("latest_urn", ""))[:255]
        upload_at = timezone.now()

        # The scanner workstation sends *absolute* (cumulative) counts on
        # every webhook. ``update_or_create`` is SELECT-then-UPDATE in
        # Python, so five concurrent POSTs can read the same prior value
        # and clobber each other's writes — losing increments. Wrapping the
        # counters in ``Greatest(F(...), Value(...))`` resolves the read +
        # write inside a single SQL statement, so concurrent writers
        # converge to ``max(incoming)`` regardless of commit order.
        with transaction.atomic():
            ScanUploadProgress.objects.get_or_create(campaign=campaign)
            ScanUploadProgress.objects.filter(campaign=campaign).update(
                total_uploaded=Greatest(F("total_uploaded"), Value(incoming_uploaded)),
                total_expected=Greatest(F("total_expected"), Value(incoming_expected)),
                latest_urn=latest_urn,
                status=raw_status,
                last_upload_at=upload_at,
                last_error=last_error,
            )

        progress = ScanUploadProgress.objects.get(campaign=campaign)

        logger.info(
            "Scan progress updated: campaign=%s, uploaded=%d, status=%s",
            campaign_id,
            progress.total_uploaded,
            progress.status,
        )

        # Auto-trigger OCR processing when upload is complete
        response_data: dict = {
            "ok": True,
            "total_uploaded": progress.total_uploaded,
        }

        if raw_status == ScanUploadProgress.STATUS_COMPLETE:
            try:
                upload_error = _handle_upload_complete(
                    data, campaign_id, progress, response_data
                )
            except IntegrityError:
                # ScanBatch ``(campaign, batch_name)`` unique constraint
                # collided — surface as 409 instead of bubbling up to a 500.
                # Returning a clear hint lets the scanner workstation operator
                # rename the file and retry without contacting support.
                logger.warning(
                    "Scan upload webhook: duplicate batch_name for campaign %s",
                    campaign_id,
                )
                return JsonResponse(
                    {
                        "error": "duplicate_batch",
                        "hint": "use a unique filename",
                    },
                    status=409,
                )
            if upload_error is not None:
                logger.warning(
                    "Scan upload completion rejected for campaign %s (status=%d): %s",
                    campaign_id,
                    upload_error.status,
                    upload_error.message,
                )
                return JsonResponse(
                    {"error": upload_error.message}, status=upload_error.status
                )

        return JsonResponse(response_data)

    except Campaign.DoesNotExist:
        return JsonResponse({"error": "Campaign not found"}, status=404)
    except Exception as e:
        logger.exception("Scan upload webhook error: %s", e)
        return JsonResponse({"error": "Internal error"}, status=500)


# ═══════════════════════════════════════════════════════════════════
# Scanner Campaigns API (read-only, HMAC-authenticated)
# ═══════════════════════════════════════════════════════════════════


def _build_campaign_list(campaigns: Iterable[Any]) -> list[dict[str, str]]:
    """Build serialized campaign list for scanner API.

    Args:
        campaigns: QuerySet of active campaigns with client prefetched.

    Returns:
        List of campaign dicts with id, name, appeal_code, client info.
    """
    result: list[dict[str, str]] = []
    for c in campaigns:
        client_name = c.client.name if c.client else "No Client"
        result.append(
            {
                "id": str(c.id),
                "name": c.name,
                "appeal_code": c.appeal_code or "",
                "client_name": client_name,
                "client_slug": client_name.lower().replace(" ", "-"),
                # client_id is required by the upload webhook — include it so
                # scanner workstations can build valid webhook payloads.
                "client_id": str(c.client_id) if c.client_id else "",
            }
        )
    return result


@csrf_exempt
def scanner_campaigns_list(request: HttpRequest) -> JsonResponse:
    """Return active campaigns for the scanner workstation menu.

    URL: /webhooks/scanner/campaigns/

    GET — returns all active campaigns grouped by client.
    Uses the same HMAC-SHA256 authentication as the scan-upload webhook
    so the scanner workstation doesn't need separate credentials.

    Headers:
        X-Signature: HMAC-SHA256 hex digest of
                     ``f"{timestamp}.{query_string}"``.
        X-Scan-Timestamp: Unix timestamp (seconds) when the request was sent.

    Returns:
        JSON: {"campaigns": [...], "payment_methods": [...]}
    """
    if request.method != "GET":
        return JsonResponse({"error": "GET only"}, status=405)

    # Verify HMAC — the signature covers ``f"{timestamp}.{query_string}"``.
    payload = (request.META.get("QUERY_STRING", "") or "").encode()
    signature = request.META.get("HTTP_X_SIGNATURE", "")
    timestamp_str = request.META.get("HTTP_X_SCAN_TIMESTAMP")
    sig_ok = (
        _verify_scan_signature(payload, signature, timestamp=timestamp_str)
        if timestamp_str is not None
        else False
    )
    if not sig_ok:
        logger.warning("Scanner campaigns API: invalid signature")
        return JsonResponse({"error": "Invalid signature"}, status=403)

    parsed_timestamp = _parse_scan_timestamp(timestamp_str)
    if parsed_timestamp is None or not _scan_timestamp_within_skew(parsed_timestamp):
        logger.warning(
            "Scanner campaigns API: timestamp missing or outside skew window"
        )
        return JsonResponse(
            {"error": "timestamp missing or outside allowed skew window"}, status=401
        )

    client_id = request.GET.get("client_id")
    if not client_id:
        return JsonResponse({"error": "client_id is required"}, status=400)

    try:
        from campaigns.models import Campaign

        campaigns = (
            Campaign.objects.filter(
                status=Campaign.STATUS_ACTIVE,
                client_id=client_id,
                client__is_active=True,
            )
            .select_related("client")
            .order_by("client__name", "name")
            .only(
                "id",
                "name",
                "appeal_code",
                "status",
                "client__id",
                "client__name",
            )
        )

        from scans.scan_constants import PAYMENT_METHOD_CHOICES

        return JsonResponse(
            {
                "campaigns": _build_campaign_list(campaigns),
                "payment_methods": [
                    {"value": v, "label": label} for v, label in PAYMENT_METHOD_CHOICES
                ],
            }
        )

    except Exception as e:
        logger.exception("Scanner campaigns API error: %s", e)
        return JsonResponse({"error": "Internal error"}, status=500)
