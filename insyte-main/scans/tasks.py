"""Celery tasks for OCR scan processing.

These tasks handle asynchronous processing of scanned donation forms:
- process_scan_batch_task: Process an entire scan batch through OCR.
- process_single_scan_task: Process one scan (used for retries).
- create_scan_batch_from_webhook_task: Create ScanBatch from webhook data.
- watch_r2_scan_folders_task: Auto-ingest new rclone-uploaded R2 folders.
- cleanup_stale_scan_progress_task: Reset stale ScanUploadProgress records.
- auto_retry_failed_scan_batches_task: Auto-retry recently failed batches.
- reset_stuck_scan_batches_task: Reset batches stuck in processing.
- finalize_scan_batch_chord_error_task: Chord error fallback (timeout/failure).
- cleanup_r2_orphans_task: Sweep unreferenced redacted-tmp/redacted/originals blobs.

All tasks are idempotent and safe to retry.
"""

import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import chord, group, shared_task
from celery.exceptions import Retry
from celery.utils.log import get_task_logger
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.db.models import F

from core.metrics import observe_scan_ocr_latency

logger = get_task_logger(__name__)


def _resolve_client_label_for_placeholder(placeholder_id: str) -> str | None:
    """Best-effort lookup of the client name for OCR latency labels.

    Used solely for Prometheus labels; failures fall back to ``None`` so
    metric emission never blocks the scan pipeline.
    """
    try:
        from scans.models import ScanPlaceholder

        placeholder = (
            ScanPlaceholder.objects.select_related("batch__campaign__client")
            .only("batch__campaign__client__name")
            .get(id=placeholder_id)
        )
        client = placeholder.batch.campaign.client
        return getattr(client, "name", None)
    except Exception:  # pragma: no cover — pure observability path
        return None


_NON_RETRYABLE_SCAN_EXCEPTIONS = (ValidationError, ValueError)

# Hard deadline for the chord header (sum of fan-out scan tasks). If the chord
# header has not produced a complete result set within this window the
# finalize callback fires via ``chord_error`` so the batch never gets stuck
# in ``processing``. 10 minutes comfortably covers a chord of ~30 placeholders
# at the per-scan ``time_limit`` of 3 minutes when running with 5 parallel
# workers; bigger fan-outs will simply finalise into ``partially_completed``.
_CHORD_DEADLINE_SECONDS = 10 * 60

# How long the finalize task waits before retrying when ``select_for_update``
# could not acquire the row lock (a per-scan retry is mid-flight). Short
# enough to catch the lock release quickly without hot-looping.
_FINALIZE_LOCK_RETRY_COUNTDOWN = 15

# Image file extensions accepted for scan processing
_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".tiff",
        ".tif",
        ".pdf",
        ".bmp",
        ".webp",
    }
)


def _filter_image_keys(r2_keys: list[str]) -> list[str]:
    """Filter R2 keys to only image/PDF files.

    Args:
        r2_keys: Raw list of R2 object keys.

    Returns:
        Filtered list containing only keys with image extensions.
    """
    return [
        key
        for key in r2_keys
        if any(key.lower().endswith(ext) for ext in _IMAGE_EXTENSIONS)
    ]


def _resolve_user(user_id: int | None) -> object | None:
    """Resolve a User instance from ID, returning None if not found.

    Args:
        user_id: Optional user ID to look up.

    Returns:
        User instance or None.
    """
    if not user_id:
        return None

    from core.models import User

    with contextlib.suppress(User.DoesNotExist):
        return User.objects.get(id=user_id)
    return None


@shared_task(
    bind=True,
    name="scans.process_scan_batch",
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=5 * 60,  # 5 minutes — orchestrator only, no OCR work
    time_limit=6 * 60,
)
def process_scan_batch_task(
    self: object,
    scan_batch_id: str,
) -> dict:
    """Orchestrate fan-out OCR processing for a scan batch.

    Prepares the batch and dispatches one process_single_scan_task per
    pending placeholder via a Celery chord.  The chord callback
    (finalize_scan_batch_task) creates the DonationBatch once all scans
    have finished.

    Args:
        self: Celery task instance.
        scan_batch_id: UUID of the ScanBatch to process.

    Returns:
        dict with dispatch summary.
    """
    from scans.scan_processing import ScanProcessingService

    logger.info("Preparing fan-out OCR processing for scan batch %s", scan_batch_id)

    try:
        # Wrap the PROCESSING status save (inside ``prepare_scan_batch``) and
        # the chord dispatch in a single transaction. Use ``on_commit`` so the
        # chord is only fired after the row commits — if anything in the
        # ``prepare_scan_batch`` call fails, the PROCESSING save rolls back
        # and the next orchestrator retry sees the batch in PENDING again.
        # This prevents a half-dispatched chord followed by a retry that
        # would otherwise dispatch a second chord on top of the first.
        with transaction.atomic():
            _scan_batch, known_urns, placeholder_ids = (
                ScanProcessingService.prepare_scan_batch(scan_batch_id)
            )

            if not placeholder_ids:
                logger.info("Scan batch %s has no pending placeholders", scan_batch_id)
                # Empty batches finalise inline; safe outside on_commit because
                # they don't fan out to other workers.
                pass
            else:
                logger.info(
                    "Dispatching %d scan task(s) for batch %s",
                    len(placeholder_ids),
                    scan_batch_id,
                )
                # Capture closures for ``on_commit`` — Django's hook fires
                # with no args after the outermost transaction commits.
                ids_for_dispatch = list(placeholder_ids)
                urns_for_dispatch = list(known_urns)
                transaction.on_commit(
                    lambda: _dispatch_scan_chord(
                        scan_batch_id,
                        urns_for_dispatch,
                        ids_for_dispatch,
                    )
                )

        if not placeholder_ids:
            return _finalise_empty_batch(scan_batch_id)

        return {
            "status": "dispatched",
            "scan_batch_id": scan_batch_id,
            "total": len(placeholder_ids),
        }

    except _NON_RETRYABLE_SCAN_EXCEPTIONS as exc:
        logger.warning("Scan batch %s non-retryable error: %s", scan_batch_id, exc)
        _mark_batch_failed(scan_batch_id, str(exc))
        return {
            "status": "error",
            "scan_batch_id": scan_batch_id,
            "message": str(exc),
            "retryable": False,
        }

    except Exception as exc:
        logger.exception("Scan batch %s orchestration failed", scan_batch_id)
        _mark_batch_failed(scan_batch_id, str(exc))
        raise self.retry(exc=exc) from exc


@shared_task(
    bind=True,
    name="scans.process_single_scan",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=120,  # 2 minutes per scan
    time_limit=180,  # 3 minutes hard limit
)
def process_single_scan_task(
    self: object,
    placeholder_id: str,
    scan_batch_id: str | None = None,
    known_urns: list[str] | None = None,
) -> dict:
    """Process a single scan placeholder through OCR.

    Used both as a chord fan-out step and for standalone retries of failed
    scans.  ``scan_batch_id`` and ``known_urns`` are optional for
    backwards-compatibility with direct retry calls.

    Args:
        self: Celery task instance.
        placeholder_id: UUID of the ScanPlaceholder to process.
        scan_batch_id: Optional parent batch UUID.  When supplied the batch
            progress counters are updated atomically after every scan.
        known_urns: Optional pre-loaded campaign URN list.  Avoids a
            redundant DB query when processing inside a fan-out chord.

    Returns:
        dict with processing result for this scan.
    """
    from scans.scan_processing import ScanProcessingService

    logger.info("Processing single scan %s", placeholder_id)

    ocr_started_at = time.perf_counter()
    try:
        result = ScanProcessingService.process_single_scan(
            placeholder_id, known_urns=known_urns
        )
        # Observability: record wall-clock latency for the OCR call so we
        # can alert on slow Document AI responses per client.
        observe_scan_ocr_latency(
            time.perf_counter() - ocr_started_at,
            client=_resolve_client_label_for_placeholder(placeholder_id),
        )
        logger.info(
            "Scan %s processed: status=%s",
            placeholder_id,
            result.get("ocr_status", "unknown"),
        )
        if scan_batch_id is not None:
            _increment_batch_progress(
                scan_batch_id, placeholder_id, result.get("ocr_status", "")
            )
        return result

    except _NON_RETRYABLE_SCAN_EXCEPTIONS as exc:
        logger.warning("Single scan %s non-retryable error: %s", placeholder_id, exc)
        return {
            "status": "error",
            "placeholder_id": placeholder_id,
            "message": str(exc),
            "retryable": False,
        }

    except Exception as exc:
        # Reached only for transient errors raised from ``process_single_scan``
        # (TransientOCRError) — every other exception is converted into a
        # FAILED-status result dict by ``_handle_scan_failure``. Distinguish
        # "still have retries" from "final attempt" so the placeholder is
        # marked FAILED before we propagate the exhausted exception.
        retries_used = int(getattr(self.request, "retries", 0) or 0)  # type: ignore[attr-defined]
        max_retries = int(getattr(self, "max_retries", 0) or 0)  # type: ignore[attr-defined]
        if retries_used >= max_retries:
            logger.exception(
                "Single scan %s transient retries exhausted — marking failed",
                placeholder_id,
            )
            _mark_placeholder_failed(placeholder_id, str(exc))
            raise
        logger.warning(
            "Single scan %s transient failure (retry %d/%d): %s",
            placeholder_id,
            retries_used + 1,
            max_retries,
            exc,
        )
        raise self.retry(exc=exc) from exc


@shared_task(
    bind=True,
    name="scans.finalize_scan_batch",
    max_retries=5,
    default_retry_delay=60,
    soft_time_limit=8 * 60,  # 8 minutes
    time_limit=10 * 60,  # 10 minutes
)
def finalize_scan_batch_task(
    self: Any,
    results: list[dict],
    scan_batch_id: str,
) -> dict:
    """Chord callback: finalise batch after all individual scan tasks complete.

    Receives the accumulated chord results, aggregates matched/failed counts,
    creates the DonationBatch, and sends the completion notification.

    Concurrency model: a per-scan retry of ``process_single_scan_task`` may
    still be writing to the parent ``ScanBatch`` row (counter increments) at
    the moment the chord callback fires. To avoid reading a torn snapshot of
    ``processed_scans`` / ``matched_scans`` we acquire a row-level lock with
    ``select_for_update(skip_locked=True)`` inside ``transaction.atomic()``.
    If a writer is holding the lock we re-queue ourselves with a short
    backoff instead of blocking the worker.

    Args:
        self: Celery task instance.
        results: Result dicts from each process_single_scan_task.  Non-dict
            entries (exception objects from exhausted retries) count as
            failures.
        scan_batch_id: UUID of the ScanBatch to finalise.

    Returns:
        dict with processing summary.
    """
    from scans.models import ScanBatch
    from scans.scan_processing import ScanProcessingService

    total, matched, failed = _aggregate_scan_results(results)
    logger.info(
        "Finalising scan batch %s: %d total, %d matched, %d failed",
        scan_batch_id,
        total,
        matched,
        failed,
    )

    try:
        with transaction.atomic():
            locked = (
                ScanBatch.objects.select_for_update(skip_locked=True)
                .filter(pk=scan_batch_id)
                .first()
            )
            if locked is None:
                # ``skip_locked`` returned an empty set: either the row no
                # longer exists or a concurrent writer holds the lock. Fall
                # through to a non-locked existence check so we can tell the
                # two apart.
                if not ScanBatch.objects.filter(pk=scan_batch_id).exists():
                    raise ValueError(f"ScanBatch not found: {scan_batch_id}")
                logger.info(
                    "finalize_scan_batch_task: row %s is locked by a "
                    "concurrent writer; re-queuing with backoff",
                    scan_batch_id,
                )
                raise self.retry(countdown=_FINALIZE_LOCK_RETRY_COUNTDOWN)

            result = ScanProcessingService.finalize_scan_batch(
                scan_batch_id, total=total, matched=matched, failed=failed
            )
        _notify_batch_complete(scan_batch_id, result)
        return result

    except _NON_RETRYABLE_SCAN_EXCEPTIONS as exc:
        logger.warning(
            "Scan batch %s finalisation non-retryable error: %s", scan_batch_id, exc
        )
        _mark_batch_failed(scan_batch_id, str(exc))
        return {
            "status": "error",
            "scan_batch_id": scan_batch_id,
            "message": str(exc),
            "retryable": False,
        }

    except Retry:
        # ``self.retry`` (e.g. the lock-contention re-queue above) raises
        # ``Retry`` to interrupt execution — let it propagate so the worker
        # re-queues us instead of treating it as a finalize failure.
        raise

    except OperationalError as exc:
        # Lock acquisition timed out at the DB layer (rare with skip_locked,
        # but possible under heavy contention). Re-queue cleanly; dispatch
        # the DB-truth-driven fallback if retries are exhausted so the
        # batch never sits forever in PROCESSING.
        logger.warning(
            "Scan batch %s finalisation hit DB lock contention: %s — retrying",
            scan_batch_id,
            exc,
        )
        _maybe_dispatch_finalize_fallback(self, scan_batch_id, exc)
        raise self.retry(exc=exc, countdown=_FINALIZE_LOCK_RETRY_COUNTDOWN) from exc

    except Exception as exc:
        logger.exception("Scan batch %s finalisation failed", scan_batch_id)
        _mark_batch_failed(scan_batch_id, str(exc))
        _maybe_dispatch_finalize_fallback(self, scan_batch_id, exc)
        raise self.retry(exc=exc) from exc


@shared_task(
    name="scans.finalize_scan_batch_chord_error",
    soft_time_limit=120,
    time_limit=180,
)
def finalize_scan_batch_chord_error_task(
    request: Any,
    exc: Any,
    traceback: Any,
    scan_batch_id: str,
) -> dict:
    """Chord error fallback: finalise a batch when the chord header fails or expires.

    Wired in via ``canvas.link_error(finalize_scan_batch_chord_error_task.s(scan_batch_id))``.
    Celery invokes this when one or more header tasks raises (after exhausting
    its own retries) or when the chord header expires past the
    ``_CHORD_DEADLINE_SECONDS`` deadline. The fallback inspects the persisted
    ScanPlaceholder rows (the source of truth) to derive matched/failed
    counts, marks any still-pending placeholders as failed, and runs the
    normal finalize flow so the batch never sits forever in ``processing``.

    Args:
        request: Celery request context for the failing task (per Celery's
            error-callback contract).
        exc: The exception raised by the chord header.
        traceback: Traceback string from the failing header task.
        scan_batch_id: UUID of the ScanBatch to finalise.

    Returns:
        dict with the finalisation summary, or an error dict if the batch
        could not be finalised.
    """
    from scans.models import ScanPlaceholder
    from scans.scan_processing import ScanProcessingService

    logger.warning(
        "Chord for scan batch %s failed/expired (exc=%r); running fallback finalize",
        scan_batch_id,
        exc,
    )

    placeholders = ScanPlaceholder.objects.filter(batch_id=scan_batch_id)

    # Mark any placeholder still mid-flight as failed so the chord-error
    # handler always converges on a terminal state.
    pending_or_processing = placeholders.filter(
        ocr_status__in=[
            ScanPlaceholder.OCR_STATUS_PENDING,
            ScanPlaceholder.OCR_STATUS_PROCESSING,
        ]
    )
    forced_failed = pending_or_processing.update(
        ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
        processing_error="Chord deadline exceeded; placeholder did not complete in time.",
    )
    if forced_failed:
        logger.warning(
            "Chord fallback marked %d placeholder(s) in batch %s as failed",
            forced_failed,
            scan_batch_id,
        )

    matched = placeholders.filter(ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED).count()
    failed = placeholders.filter(ocr_status=ScanPlaceholder.OCR_STATUS_FAILED).count()
    total = placeholders.count()

    try:
        result = ScanProcessingService.finalize_scan_batch(
            scan_batch_id, total=total, matched=matched, failed=failed
        )
        _notify_batch_complete(scan_batch_id, result)
        return result
    except Exception as fallback_exc:
        if isinstance(fallback_exc, _NON_RETRYABLE_SCAN_EXCEPTIONS):
            logger.warning(
                "Chord fallback finalize for %s failed: %s",
                scan_batch_id,
                fallback_exc,
            )
        else:
            logger.exception(
                "Chord fallback finalize for %s raised unexpectedly", scan_batch_id
            )
        _mark_batch_failed(scan_batch_id, str(fallback_exc))
        return {
            "status": "error",
            "scan_batch_id": scan_batch_id,
            "message": str(fallback_exc),
            "retryable": False,
        }


@shared_task(
    bind=True,
    name="scans.create_scan_batch_from_r2",
    max_retries=2,
    default_retry_delay=30,
    retry_backoff=True,
    retry_jitter=True,
)
def create_scan_batch_from_r2_task(
    self: object,
    campaign_id: str,
    r2_prefix: str,
    payment_method: str,
    scan_form_type: str,
    batch_name: str = "",
    user_id: int | None = None,
    auto_process: bool = True,
) -> dict:
    """Create a ScanBatch from R2 files matching a prefix, then optionally process.

    Called when scanner uploads are complete (webhook status=complete).
    Lists R2 objects under the given prefix, creates a ScanBatch with
    ScanPlaceholder records, and optionally triggers OCR processing.

    For the physical-batch workflow, the prefix must contain exactly one
    uploaded PDF whose filename becomes the canonical batch identifier.

    Args:
        self: Celery task instance.
        campaign_id: UUID of the campaign.
        r2_prefix: R2 key prefix to list (e.g. "client/appeal/cheque/").
        payment_method: Payment method for the batch.
        scan_form_type: Physical document layout for the batch.
        batch_name: Optional batch name that must match the uploaded PDF filename.
        user_id: ID of the user who triggered this (optional).
        auto_process: If True, automatically trigger OCR processing.

    Returns:
        dict with scan_batch_id and status.
    """
    from core.storage_backends import R2ConfigurationError, r2_list_prefix
    from scans.scan_processing import ScanProcessingService

    logger.info(
        "Creating scan batch from R2 prefix '%s' for campaign %s",
        r2_prefix,
        campaign_id,
    )

    try:
        # An empty list here means the bucket really had no matching keys;
        # transient R2 failures propagate from r2_list_prefix for Celery retry.
        r2_keys = _filter_image_keys(r2_list_prefix(r2_prefix, max_keys=500))

        if not r2_keys:
            logger.warning("No image files found under R2 prefix '%s'", r2_prefix)
            # Surface the failure on the campaign's progress row so the
            # operator/UI sees that the upload landed in a dead-end. Without
            # this the webhook's 24-hour completion-dedup hash silently
            # blocks the scanner from re-dispatching, with no visible cause.
            _record_no_files_failure(campaign_id, r2_prefix)
            return {
                "status": "no_files",
                "message": f"No image files found under prefix: {r2_prefix}",
            }

        user = _resolve_user(user_id)

        # With Patch T splitting, each R2 key is one donor's document.
        # No duplex splitting needed — pass all keys directly.
        scan_batch = ScanProcessingService.create_scan_batch_from_r2(
            campaign_id=campaign_id,
            r2_keys=r2_keys,
            payment_method=payment_method,
            scan_form_type=scan_form_type,
            user=user,
            batch_name=batch_name,
        )

        result: dict[str, object] = {
            "status": "created",
            "scan_batch_id": str(scan_batch.id),
            "total_scans": int(scan_batch.total_scans),
        }

        # Auto-trigger OCR processing
        if auto_process:
            process_scan_batch_task.delay(str(scan_batch.id))
            result["processing_triggered"] = True
            logger.info(
                "Auto-triggered OCR processing for scan batch %s", scan_batch.id
            )

        return result

    except R2ConfigurationError as exc:
        logger.error(
            "R2 misconfiguration while listing prefix '%s': %s — not retrying",
            r2_prefix,
            exc,
        )
        return {
            "status": "error",
            "message": str(exc),
            "retryable": False,
        }

    except _NON_RETRYABLE_SCAN_EXCEPTIONS as exc:
        logger.warning(
            "Invalid scan batch request for prefix '%s': %s",
            r2_prefix,
            exc,
        )
        return {
            "status": "error",
            "message": str(exc),
            "retryable": False,
        }

    except Exception as exc:
        logger.exception("Failed to create scan batch from R2 prefix '%s'", r2_prefix)
        raise self.retry(exc=exc) from exc


@shared_task(
    bind=True,
    name="scans.apply_cvv_redaction",
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,
    retry_jitter=True,
    soft_time_limit=120,
    time_limit=180,
)
def apply_cvv_redaction_task(self: Any, placeholder_id: str) -> dict:
    """Apply the CVV-only blackout layer to a placeholder's R2 image.

    Fires immediately on every Stripe ``PaymentIntent.create()`` return
    (success, requires_action, requires_payment_method, failure) and on the
    ``payment_intent.payment_failed`` / ``payment_intent.requires_action``
    webhook events. PCI DSS Requirement 3.2 forbids storing sensitive
    authentication data after authorization — so the CVV pass must run on
    the auth attempt itself, not "once a charge eventually succeeds."

    Idempotent: a no-op when the CVV pass has already run (status past
    ``REDACTION_CVV_PENDING``) or no CVV coords are saved. Transient R2
    failures retry; ValueError (bad coords) is terminal.
    """
    from scans.models import ScanPlaceholder
    from scans.scan_redaction import apply_cvv_redaction

    try:
        placeholder = ScanPlaceholder.objects.get(id=placeholder_id)
    except ScanPlaceholder.DoesNotExist:
        logger.warning(
            "apply_cvv_redaction_task: placeholder %s not found", placeholder_id
        )
        return {"status": "not_found", "placeholder_id": placeholder_id}

    try:
        applied = apply_cvv_redaction(placeholder)
    except ValueError as exc:
        logger.warning(
            "apply_cvv_redaction_task: invalid request for placeholder %s: %s",
            placeholder_id,
            exc,
        )
        return {
            "status": "error",
            "placeholder_id": placeholder_id,
            "message": str(exc),
            "retryable": False,
        }
    except Exception as exc:
        logger.exception(
            "apply_cvv_redaction_task: transient failure for placeholder %s",
            placeholder_id,
        )
        raise self.retry(exc=exc) from exc

    return {
        "status": "applied" if applied else "noop",
        "placeholder_id": placeholder_id,
    }


@shared_task(
    bind=True,
    name="scans.apply_deferred_redaction",
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,
    retry_jitter=True,
    soft_time_limit=120,
    time_limit=180,
)
def apply_deferred_redaction_task(self: Any, placeholder_id: str) -> dict:
    """Apply the post-charge blackout layer to a placeholder's R2 image.

    Fired after a successful Stripe charge (sync hook in BatchPaymentService
    and ``payment_intent.succeeded`` webhook), from the operator-marks-failed
    path, and from the retention TTL sweeper. Acts as the safety net for the
    CVV layer too — if ``apply_cvv_redaction`` never ran for any reason, this
    task applies CVV first inside the same row lock before applying the
    post-charge layer.

    Idempotent: a no-op when the placeholder is already
    ``REDACTION_COMPLETED``. Transient R2 failures retry; ValueError is
    terminal.
    """
    from scans.models import ScanPlaceholder
    from scans.scan_redaction import apply_deferred_redaction

    try:
        placeholder = ScanPlaceholder.objects.get(id=placeholder_id)
    except ScanPlaceholder.DoesNotExist:
        logger.warning(
            "apply_deferred_redaction_task: placeholder %s not found", placeholder_id
        )
        return {"status": "not_found", "placeholder_id": placeholder_id}

    try:
        applied = apply_deferred_redaction(placeholder)
    except ValueError as exc:
        logger.warning(
            "apply_deferred_redaction_task: invalid request for placeholder %s: %s",
            placeholder_id,
            exc,
        )
        return {
            "status": "error",
            "placeholder_id": placeholder_id,
            "message": str(exc),
            "retryable": False,
        }
    except Exception as exc:
        logger.exception(
            "apply_deferred_redaction_task: transient failure for placeholder %s",
            placeholder_id,
        )
        raise self.retry(exc=exc) from exc

    return {
        "status": "applied" if applied else "noop",
        "placeholder_id": placeholder_id,
    }


@shared_task(
    name="scans.retry_failed_scans",
    soft_time_limit=15 * 60,
    time_limit=20 * 60,
)
def retry_failed_scans_task(scan_batch_id: str) -> dict:
    """Retry all failed scans in a batch.

    Resets failed ScanPlaceholders to pending and re-processes them.
    IDs are captured **before** the status update so that the queryset
    re-evaluation after ``.update()`` does not return an empty set.

    Args:
        scan_batch_id: UUID of the ScanBatch.

    Returns:
        dict with retry summary.
    """
    from scans.models import ScanBatch, ScanPlaceholder
    from scans.scan_processing import ScanProcessingService

    failed_qs = ScanPlaceholder.objects.filter(
        batch_id=scan_batch_id,
        ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
    )

    # Capture IDs BEFORE the update so the queryset is not re-evaluated
    # after the status change (which would return empty results).
    # A placeholder may have failed late in the pipeline after a Donation was
    # already created on a previous attempt; resetting and re-running such a
    # placeholder would create a duplicate Donation, so partition into
    # retryable vs skipped groups up-front.
    failed_rows: list[tuple[Any, Any]] = list(
        failed_qs.values_list("id", "donation_id")
    )
    count = len(failed_rows)

    if count == 0:
        logger.info(
            "retry_failed_scans_task: no failed scans in batch %s", scan_batch_id
        )
        return {
            "status": "no_failed_scans",
            "retried": 0,
            "skipped_with_existing_donation": [],
        }

    skipped_ids: list[str] = [
        str(pid) for pid, donation_id in failed_rows if donation_id is not None
    ]
    retryable_ids: list[str] = [
        str(pid) for pid, donation_id in failed_rows if donation_id is None
    ]

    if skipped_ids:
        logger.warning(
            "retry_failed_scans_task: skipping %d placeholder(s) in batch %s "
            "with an existing Donation: %s",
            len(skipped_ids),
            scan_batch_id,
            skipped_ids,
        )

    if not retryable_ids:
        logger.info(
            "retry_failed_scans_task: all %d failed scan(s) in batch %s already "
            "have donations; nothing to retry",
            count,
            scan_batch_id,
        )
        return {
            "status": "skipped_all",
            "retried": 0,
            "total_failed": count,
            "skipped_with_existing_donation": skipped_ids,
        }

    logger.info(
        "retry_failed_scans_task: retrying %d failed scan(s) in batch %s "
        "(%d skipped with existing donation)",
        len(retryable_ids),
        scan_batch_id,
        len(skipped_ids),
    )

    # Reset failed placeholders (without an existing Donation) to pending so
    # they can be re-processed.
    ScanPlaceholder.objects.filter(id__in=retryable_ids).update(
        ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
        processing_error="",
    )

    # Mark batch as PROCESSING so the UI reflects activity and the folder
    # watcher knows this batch is being handled.
    ScanBatch.objects.filter(id=scan_batch_id).update(
        status=ScanBatch.STATUS_PROCESSING,
    )

    # Process each failed scan individually
    new_status = ScanBatch.STATUS_PROCESSING
    retried = 0
    for ph_id in retryable_ids:
        try:
            ScanProcessingService.process_single_scan(str(ph_id))
            retried += 1
        except Exception:
            logger.exception("Failed to re-process scan %s during retry", ph_id)

    # Refresh batch statistics and final status after retry
    with contextlib.suppress(ScanBatch.DoesNotExist):
        batch = ScanBatch.objects.get(id=scan_batch_id)

        # Re-calculate counts from absolute source of truth (placeholders)
        total_placeholders = ScanPlaceholder.objects.filter(batch_id=scan_batch_id)

        remaining_failed = total_placeholders.filter(
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED
        ).count()

        pending_count = total_placeholders.filter(
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING
        ).count()

        matched_count = total_placeholders.filter(
            ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        ).count()

        # A scan is "processed" if it's no longer pending.
        processed_count = total_placeholders.exclude(
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING
        ).count()

        # Update final status: if anything is still pending/processing, keep it as such.
        if pending_count > 0:
            new_status = ScanBatch.STATUS_PROCESSING
        else:
            new_status = ScanBatch.final_status_for_outcomes(
                total=processed_count,
                matched=matched_count,
                failed=remaining_failed,
            )

        batch.status = new_status
        batch.processed_scans = processed_count
        batch.matched_scans = matched_count
        batch.save(
            update_fields=["status", "processed_scans", "matched_scans", "updated_at"]
        )

        # After saving final stats, check if we need to generate/update Donations
        if new_status in (
            ScanBatch.STATUS_COMPLETED,
            ScanBatch.STATUS_PARTIALLY_COMPLETED,
            ScanBatch.STATUS_FAILED,
        ):
            if not batch.donation_batch:
                # The entire batch failed its first run and no DonationBatch was ever made.
                # Generate it now from whichever scans succeeded during retry.
                donation_batch = ScanProcessingService._create_donation_batch(batch)
                if donation_batch:
                    batch.donation_batch = donation_batch
                    batch.save(update_fields=["donation_batch", "updated_at"])
            else:
                # DonationBatch already exists. Find any newly succeeded scans
                # that don't have a linked Donation yet, and add them.
                # Issue 8: use select_for_update(skip_locked=True) inside a
                # transaction so concurrent retry tasks cannot create duplicate
                # donations for the same placeholder.
                from django.db import transaction as _tx

                from donations.models import Donation

                with _tx.atomic():
                    newly_matched = (
                        total_placeholders.filter(
                            ocr_status__in=[
                                ScanPlaceholder.OCR_STATUS_MATCHED,
                                ScanPlaceholder.OCR_STATUS_UNMATCHED,
                                ScanPlaceholder.OCR_STATUS_COMPLETED,
                            ],
                            donation__isnull=True,
                        )
                        .select_for_update(skip_locked=True)
                        .select_related("matched_donor", "matched_data_file_donor")
                    )

                    if newly_matched.exists():
                        donations_created = 0
                        for ph in newly_matched:
                            ScanProcessingService._create_donation_from_placeholder(
                                ph, batch.campaign, batch.donation_batch, batch
                            )
                            donations_created += 1

                        if donations_created > 0:
                            from decimal import Decimal

                            from django.db import models

                            dbatch = batch.donation_batch
                            # Recompute from authoritative row counts: any
                            # prior drift (manual edits, GDPR erase, partial
                            # failures) won't compound across re-runs.
                            dbatch.total_donations = Donation.objects.filter(
                                batch=dbatch
                            ).count()
                            dbatch.total_amount = Donation.objects.filter(
                                batch=dbatch
                            ).aggregate(total=models.Sum("amount"))["total"] or Decimal(
                                "0.00"
                            )
                            dbatch.save(
                                update_fields=[
                                    "total_donations",
                                    "total_amount",
                                    "updated_at",
                                ]
                            )

    logger.info(
        "retry_failed_scans_task: complete — %d/%d scans retried in batch %s "
        "(%d skipped with existing donation), final status: %s",
        retried,
        count,
        scan_batch_id,
        len(skipped_ids),
        new_status,
    )

    return {
        "status": "retried",
        "retried": retried,
        "total_failed": count,
        "skipped_with_existing_donation": skipped_ids,
    }


# ── Fan-out helpers ────────────────────────────────────────────────────────


def _finalise_empty_batch(scan_batch_id: str) -> dict:
    """Immediately finalise a batch that has no pending placeholders.

    Args:
        scan_batch_id: UUID of the ScanBatch to finalise.

    Returns:
        dict with processing summary.
    """
    from scans.scan_processing import ScanProcessingService

    result = ScanProcessingService.finalize_scan_batch(
        scan_batch_id, total=0, matched=0, failed=0
    )
    _notify_batch_complete(scan_batch_id, result)
    return result


def _dispatch_scan_chord(
    scan_batch_id: str,
    known_urns: list[str],
    placeholder_ids: list[str],
) -> None:
    """Build and fire the chord that processes each placeholder in parallel.

    The header (one ``process_single_scan_task`` per placeholder) is given an
    explicit ``expires`` deadline so that header tasks revoked past the
    deadline trigger the chord error callback. The error callback
    (``finalize_scan_batch_chord_error_task``) finalises the batch using the
    DB state of the placeholders, so a hung worker can never leave a batch
    stuck in ``processing``.

    Args:
        scan_batch_id: UUID of the parent ScanBatch.
        known_urns: Pre-loaded campaign URN list passed to every scan task.
        placeholder_ids: UUIDs of ScanPlaceholders to process.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=_CHORD_DEADLINE_SECONDS)
    # Per-task ``expires`` is what propagates to chord header tasks — passing
    # ``expires`` to ``apply_async`` on the chord canvas itself only marks
    # the body, which never runs if the header is hung anyway.
    header = group(
        process_single_scan_task.s(ph_id, scan_batch_id, known_urns).set(
            expires=deadline
        )
        for ph_id in placeholder_ids
    )
    body = finalize_scan_batch_task.s(scan_batch_id)
    canvas = chord(header, body)
    canvas.link_error(finalize_scan_batch_chord_error_task.s(scan_batch_id))
    canvas.apply_async()


def _increment_batch_progress(
    scan_batch_id: str, placeholder_id: str, ocr_status: str
) -> None:
    """Atomically increment ScanBatch progress counters after one scan completes.

    Idempotent on ``(scan_batch_id, placeholder_id)``: if a Celery retry of
    ``process_single_scan_task`` re-invokes this for the same placeholder, the
    counters are *not* incremented again. The applied keys are tracked in
    ``ScanBatch.processed_placeholder_ids``; the read-check-append-increment
    sequence runs inside ``transaction.atomic()`` with ``select_for_update()``
    on the batch row, so concurrent chord workers cannot race past the guard.

    Args:
        scan_batch_id: UUID of the ScanBatch to update.
        placeholder_id: UUID of the ScanPlaceholder that just finished. Used
            as the idempotency key for this increment.
        ocr_status: OCR status of the just-processed ScanPlaceholder.
    """
    from django.db import transaction

    from scans.models import ScanBatch, ScanPlaceholder

    key = str(placeholder_id)
    is_matched = ocr_status == ScanPlaceholder.OCR_STATUS_MATCHED

    with transaction.atomic():
        batch = (
            ScanBatch.objects.select_for_update()
            .only("id", "processed_placeholder_ids")
            .filter(id=scan_batch_id)
            .first()
        )
        if batch is None:
            logger.warning(
                "Progress increment requested for missing scan batch %s "
                "(placeholder %s)",
                scan_batch_id,
                key,
            )
            return

        applied: list[str] = list(batch.processed_placeholder_ids or [])
        if key in applied:
            logger.info(
                "Skipping duplicate progress increment for placeholder %s in "
                "batch %s (idempotency guard)",
                key,
                scan_batch_id,
            )
            return

        applied.append(key)
        ScanBatch.objects.filter(id=scan_batch_id).update(
            processed_scans=F("processed_scans") + 1,
            matched_scans=F("matched_scans") + (1 if is_matched else 0),
            processed_placeholder_ids=applied,
        )


def _aggregate_scan_results(results: list[Any]) -> tuple[int, int, int]:
    """Aggregate chord results into ``(total, matched, failed)`` counts.

    Non-dict entries (exception objects from tasks that exhausted retries)
    are treated as failed scans. Any dict result that is neither matched nor
    failed is intentionally left in the unresolved bucket via ``total``.

    Args:
        results: Raw result list from the Celery chord group.

    Returns:
        Tuple of ``(total, matched, failed)``.
    """
    from scans.models import ScanPlaceholder

    matched = sum(
        1
        for r in results
        if isinstance(r, dict)
        and r.get("ocr_status") == ScanPlaceholder.OCR_STATUS_MATCHED
    )
    failed = sum(
        1
        for r in results
        if not isinstance(r, dict)
        or r.get("ocr_status") == ScanPlaceholder.OCR_STATUS_FAILED
    )
    return len(results), matched, failed


def _mark_batch_failed(scan_batch_id: str, error_message: str) -> None:
    """Mark a scan batch as failed.

    Args:
        scan_batch_id: UUID of the ScanBatch.
        error_message: Error description.
    """
    from scans.models import ScanBatch

    try:
        ScanBatch.objects.filter(id=scan_batch_id).update(
            status=ScanBatch.STATUS_FAILED,
            error_message=error_message[:2000],
        )
    except Exception:
        logger.exception("Failed to mark scan batch %s as failed", scan_batch_id)


def _maybe_dispatch_finalize_fallback(
    task_self: Any, scan_batch_id: str, exc: BaseException
) -> None:
    """On the *last* retry, dispatch the chord-error fallback explicitly.

    ``finalize_scan_batch_chord_error_task`` normally fires from
    ``chord.link_error`` when a header task fails. It will *not* fire when
    the chord body itself (``finalize_scan_batch_task``) exhausts retries —
    so without this, a persistently-locked finalize task could exhaust its
    budget and the batch would sit forever in ``PROCESSING`` (with the
    DonationBatch never created). Dispatching the fallback explicitly hands
    finalisation to the DB-truth-driven path, which is now safe to invoke
    twice thanks to ``create_donation_batch``'s idempotency guard.
    """
    retries_used = int(getattr(task_self.request, "retries", 0) or 0)
    max_retries = int(getattr(task_self, "max_retries", 0) or 0)
    if retries_used < max_retries:
        return
    try:
        finalize_scan_batch_chord_error_task.apply_async(
            args=(None, repr(exc), "", scan_batch_id),
        )
        logger.warning(
            "finalize_scan_batch_task: retries exhausted for %s — dispatched "
            "chord-error fallback",
            scan_batch_id,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch chord-error fallback for scan batch %s",
            scan_batch_id,
        )


def _record_no_files_failure(campaign_id: str, r2_prefix: str) -> None:
    """Surface an empty-prefix completion as an operator-visible error.

    The scan-completion webhook commits a 24-hour dedup hash *before* this
    Celery task runs (see ``core/webhooks.py:_handle_upload_complete``), so a
    silent ``no_files`` outcome would block scanner retries with no visible
    cause. Update the campaign's ``ScanUploadProgress`` row to ``error`` so
    the dashboard surfaces the dead end and the operator can clear the
    stuck completion-hash manually if needed.
    """
    from scans.models import ScanUploadProgress

    try:
        ScanUploadProgress.objects.filter(campaign_id=campaign_id).update(
            status=ScanUploadProgress.STATUS_ERROR,
            last_error=(
                f"No image files found under R2 prefix '{r2_prefix}'. "
                "The completion webhook is deduped for 24h — clear "
                "ScanUploadProgress.last_completion_request_hash to allow "
                "the scanner to re-dispatch."
            )[:2000],
        )
    except Exception:
        logger.exception(
            "Failed to record no-files failure on ScanUploadProgress for "
            "campaign %s prefix %s",
            campaign_id,
            r2_prefix,
        )


def _mark_placeholder_failed(placeholder_id: str, error_message: str) -> None:
    """Mark a single ScanPlaceholder as ``OCR_STATUS_FAILED``.

    Used when the per-scan Celery task exhausts its retry budget on a
    transient error: the placeholder must converge on FAILED so the chord
    aggregator counts it correctly and ``retry_failed_scans_task`` can
    attempt a manual retry later.
    """
    from scans.models import ScanPlaceholder

    try:
        ScanPlaceholder.objects.filter(id=placeholder_id).update(
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error=error_message[:1000],
        )
    except Exception:
        logger.exception("Failed to mark placeholder %s as failed", placeholder_id)


def _notify_batch_complete(scan_batch_id: str, result: dict) -> None:
    """Send email notification when batch processing completes.

    Args:
        scan_batch_id: UUID of the ScanBatch.
        result: Processing result summary.
    """
    from scans.models import ScanBatch

    try:
        batch = ScanBatch.objects.select_related("created_by", "campaign").get(
            id=scan_batch_id
        )
        if batch.created_by and batch.created_by.email:
            from core.tasks import send_batch_status_email

            subject = f"Scan Batch '{batch.batch_name}' Processing Complete"
            html_message = (
                f"<h3>Scan Batch Processing Complete</h3>"
                f"<p>Batch: <strong>{batch.batch_name}</strong></p>"
                f"<p>Campaign: {batch.campaign.name}</p>"
                f"<p>Total Scans: {result.get('total', 0)}</p>"
                f"<p>Matched: {result.get('matched', 0)}</p>"
                f"<p>Failed: {result.get('failed', 0)}</p>"
                f"<p>A draft DonationBatch has been created with "
                f"pre-filled donations ready for verification.</p>"
            )
            send_batch_status_email.delay(
                batch.created_by.email,
                subject,
                html_message,
            )
    except Exception:
        logger.exception("Failed to send notification for scan batch %s", scan_batch_id)


# ═══════════════════════════════════════════════════════════════════
# Periodic Maintenance Tasks
# ═══════════════════════════════════════════════════════════════════


@shared_task(
    name="scans.cleanup_stale_scan_progress",
    soft_time_limit=120,
    time_limit=180,
)
def cleanup_stale_scan_progress_task() -> dict:
    """Reset ScanUploadProgress records stuck in 'scanning' status.

    If the scanner workstation crashes or loses connectivity, the
    ScanUploadProgress record will remain in 'scanning' status
    indefinitely. This task resets records that haven't received
    an update in over 2 hours to 'error' status.

    Returns:
        dict with count of cleaned up records.
    """
    from django.utils import timezone

    from scans.models import ScanUploadProgress

    cutoff = timezone.now() - timedelta(hours=2)
    stale_qs = ScanUploadProgress.objects.filter(
        status=ScanUploadProgress.STATUS_SCANNING,
        last_upload_at__lt=cutoff,
    )
    count = stale_qs.update(status=ScanUploadProgress.STATUS_ERROR)

    if count > 0:
        logger.info(
            "Cleaned up %d stale scan progress record(s) (no update for >2h)",
            count,
        )

    return {"status": "ok", "cleaned_up": count}


@shared_task(
    name="scans.auto_retry_failed_scan_batches",
    soft_time_limit=10 * 60,
    time_limit=15 * 60,
)
def auto_retry_failed_scan_batches_task() -> dict:
    """Auto-retry ScanBatch records that failed within the last 24 hours.

    Only retries batches that contain failed ScanPlaceholders and
    limits to one automatic retry per batch (checks retry_count).
    Uses retry_failed_scans_task for the actual retry logic.

    Returns:
        dict with count of batches queued for retry.
    """
    from django.db.models import Count, Q
    from django.utils import timezone

    from scans.models import ScanBatch, ScanPlaceholder

    cutoff = timezone.now() - timedelta(hours=24)
    failed_batches = (
        ScanBatch.objects.filter(
            status__in=[
                ScanBatch.STATUS_FAILED,
                ScanBatch.STATUS_PARTIALLY_COMPLETED,
            ],
            updated_at__gte=cutoff,
        )
        .annotate(
            failed_scan_count=Count(
                "placeholders",
                filter=Q(placeholders__ocr_status=ScanPlaceholder.OCR_STATUS_FAILED),
            )
        )
        .filter(failed_scan_count__gt=0)
    )

    retried = 0
    for batch in failed_batches[:10]:  # cap at 10 batches per run
        retry_failed_scans_task.delay(str(batch.id))
        retried += 1
        logger.info(
            "Auto-queued retry for failed scan batch %s (%d failed scans)",
            batch.id,
            batch.failed_scan_count,  # type: ignore[attr-defined]
        )

    return {"status": "ok", "retried": retried}


# Sweep window must be longer than ``_CHORD_DEADLINE_SECONDS`` (10 min) +
# per-scan ``time_limit`` (3 min) so a healthy chord always converges via
# its terminal state or the chord-error fallback before this sweep fires.
_STUCK_BATCH_THRESHOLD = timedelta(minutes=15)


@shared_task(
    name="scans.reset_stuck_scan_batches",
    soft_time_limit=120,
    time_limit=180,
)
def reset_stuck_scan_batches_task() -> dict:
    """Reset ScanBatch records stuck in 'processing' status.

    If the Celery worker crashes during OCR processing — or, more subtly,
    crashes between ``prepare_scan_batch`` (which marks the batch
    PROCESSING) and chord dispatch — both the batch *and* its placeholders
    can be left wedged. This task:

    1. Marks any batch processing for over ``_STUCK_BATCH_THRESHOLD``
       (15 minutes) as ``FAILED`` so the UI / retry sweepers see it.
    2. Flips any placeholder in those batches that is still ``PENDING``
       or ``PROCESSING`` to ``FAILED`` so
       ``auto_retry_failed_scan_batches_task`` / ``retry_failed_scans_task``
       (which only look at ``FAILED``) can pick them up. Without step 2,
       pre-chord-dispatch crashes leave placeholders in ``PROCESSING``
       forever — invisible to every retry sweeper.

    Returns:
        dict with count of reset batches and recovered placeholders.
    """
    from django.utils import timezone

    from scans.models import ScanBatch, ScanPlaceholder

    cutoff = timezone.now() - _STUCK_BATCH_THRESHOLD
    stuck_batch_ids = list(
        ScanBatch.objects.filter(
            status=ScanBatch.STATUS_PROCESSING,
            updated_at__lt=cutoff,
        ).values_list("id", flat=True)
    )
    if not stuck_batch_ids:
        return {"status": "ok", "reset": 0, "placeholders_recovered": 0}

    # Atomic sweep: a crash between the batch UPDATE and the placeholder
    # UPDATE would otherwise leave batches FAILED with placeholders still
    # PROCESSING — invisible to ``retry_failed_scans_task``.
    with transaction.atomic():
        batch_count = ScanBatch.objects.filter(id__in=stuck_batch_ids).update(
            status=ScanBatch.STATUS_FAILED
        )
        placeholder_count = ScanPlaceholder.objects.filter(
            batch_id__in=stuck_batch_ids,
            ocr_status__in=[
                ScanPlaceholder.OCR_STATUS_PENDING,
                ScanPlaceholder.OCR_STATUS_PROCESSING,
            ],
        ).update(
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error=(
                "Batch reset by stuck-batch sweep — placeholder did not reach "
                "a terminal state within the orchestration deadline."
            ),
        )

    logger.warning(
        "Reset %d stuck scan batch(es) and recovered %d wedged placeholder(s)",
        batch_count,
        placeholder_count,
    )

    return {
        "status": "ok",
        "reset": batch_count,
        "placeholders_recovered": placeholder_count,
    }


@shared_task(
    name="scans.apply_stale_deferred_redactions",
    soft_time_limit=10 * 60,
    time_limit=15 * 60,
)
def apply_stale_deferred_redactions_task() -> dict:
    """Sweep stuck deferred redactions and apply them after the retention TTL.

    Runs hourly via Celery beat. For every ScanPlaceholder whose
    ``redaction_status`` is ``CVV_PENDING`` or ``DEFERRED`` and whose
    ``updated_at`` is older than ``INSYTE_REDACTION_RETENTION_DAYS``, fire
    the post-charge apply task. The post-charge task itself safety-nets the
    CVV pass, so a single dispatch covers both layers.

    This is the backstop for cases that never converge naturally — donor
    abandons SCA, operator forgets to reject, queue dispatch from the sync
    path was lost. PCI DSS expects a documented retention policy with strict
    limits; this task enforces it.

    Returns:
        dict with ``status``, ``swept`` count, and the cutoff timestamp used.
    """
    from django.conf import settings
    from django.utils import timezone

    from scans.models import ScanPlaceholder

    retention_days = int(getattr(settings, "INSYTE_REDACTION_RETENTION_DAYS", 7))
    cutoff = timezone.now() - timedelta(days=retention_days)

    stale_ids: list[str] = list(
        ScanPlaceholder.objects.filter(
            redaction_status__in=[
                ScanPlaceholder.REDACTION_CVV_PENDING,
                ScanPlaceholder.REDACTION_DEFERRED,
            ],
            updated_at__lt=cutoff,
        ).values_list("id", flat=True)
    )

    if not stale_ids:
        return {
            "status": "ok",
            "swept": 0,
            "cutoff": cutoff.isoformat(),
        }

    for placeholder_id in stale_ids:
        try:
            apply_deferred_redaction_task.delay(str(placeholder_id))
        except Exception:
            logger.exception(
                "apply_stale_deferred_redactions_task: failed to enqueue "
                "post-charge redaction for placeholder %s",
                placeholder_id,
            )

    logger.warning(
        "apply_stale_deferred_redactions_task: enqueued %d placeholder(s) past "
        "the %d-day retention TTL (cutoff: %s)",
        len(stale_ids),
        retention_days,
        cutoff.isoformat(),
    )

    return {
        "status": "ok",
        "swept": len(stale_ids),
        "cutoff": cutoff.isoformat(),
    }


# ── Orphan blob prefixes scanned by cleanup_r2_orphans_task ──────────────
# ``redacted-tmp/`` is the staging area for the atomic redaction swap; any
# entry there older than the grace window is by definition a failed swap.
# ``redacted/`` and ``originals/`` are conservative "could-be-junk" zones —
# we still cross-check every key against the live ScanPlaceholder fields
# before deleting, so legacy uploads (sibling keys outside these prefixes)
# are never touched.
_R2_ORPHAN_CLEANUP_PREFIXES = ("redacted-tmp/", "redacted/", "originals/")
_R2_ORPHAN_CLEANUP_GRACE_HOURS = 24
_R2_ORPHAN_CLEANUP_MAX_KEYS = 10_000


def _collect_referenced_r2_keys() -> set[str]:
    """Return every R2 key currently referenced by a ScanPlaceholder.

    Pulls from ``page_keys``, ``original_page_keys``, ``image_path``, and
    ``donor_pdf_key`` so a key only counts as "orphan" once it has fallen
    out of every audit/display field.
    """
    from scans.models import ScanPlaceholder

    referenced: set[str] = set()
    rows = ScanPlaceholder.objects.values(
        "page_keys", "original_page_keys", "image_path", "donor_pdf_key"
    ).iterator()
    for row in rows:
        candidates: list[Any] = [
            *(row.get("page_keys") or []),
            *(row.get("original_page_keys") or []),
            row.get("image_path"),
            row.get("donor_pdf_key"),
        ]
        referenced.update(c for c in candidates if isinstance(c, str) and c)
    return referenced


@shared_task(
    name="scans.cleanup_r2_orphans",
    soft_time_limit=10 * 60,
    time_limit=15 * 60,
)
def cleanup_r2_orphans_task() -> dict:
    """Delete unreferenced R2 blobs under the redaction-related prefixes.

    Runs every 6 hours. Walks ``redacted-tmp/``, ``redacted/``, and
    ``originals/`` and deletes every object that:

    * is not referenced by any ``ScanPlaceholder`` (``page_keys``,
      ``original_page_keys``, ``image_path``, or ``donor_pdf_key``), AND
    * is older than 24 hours.

    The 24-hour grace window is the safety margin: a freshly-written temp
    blob from the atomic redaction swap may not yet have made it into a
    ``ScanPlaceholder.page_keys`` row when this task lists the bucket, so
    deleting young temp blobs would race against in-flight uploads.

    Returns:
        dict with ``status``, ``deleted`` (count), ``bytes_freed``, and
        per-prefix breakdown for log/monitoring use.
    """
    from django.utils import timezone

    from core.storage_backends import (
        R2ConfigurationError,
        r2_delete_object,
        r2_enabled,
        r2_list_prefix_with_metadata,
    )

    if not r2_enabled():
        logger.info("cleanup_r2_orphans_task: R2 not configured — nothing to do")
        return {
            "status": "skipped",
            "deleted": 0,
            "bytes_freed": 0,
            "reason": "r2_not_configured",
        }

    referenced = _collect_referenced_r2_keys()
    cutoff = timezone.now() - timedelta(hours=_R2_ORPHAN_CLEANUP_GRACE_HOURS)
    # Boto returns LastModified as UTC-aware; defend against any future
    # naive-datetime regression so we never compare aware vs naive.
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)

    deleted_total = 0
    bytes_freed_total = 0
    per_prefix: dict[str, dict[str, int]] = {}

    for prefix in _R2_ORPHAN_CLEANUP_PREFIXES:
        deleted_here = 0
        bytes_here = 0
        try:
            entries = r2_list_prefix_with_metadata(
                prefix, max_keys=_R2_ORPHAN_CLEANUP_MAX_KEYS
            )
        except R2ConfigurationError:
            logger.exception(
                "cleanup_r2_orphans_task: non-retryable R2 failure on '%s'", prefix
            )
            per_prefix[prefix] = {"deleted": 0, "bytes_freed": 0, "errored": 1}
            continue
        except Exception:
            logger.exception(
                "cleanup_r2_orphans_task: transient R2 failure on '%s' — "
                "skipping this prefix this run",
                prefix,
            )
            per_prefix[prefix] = {"deleted": 0, "bytes_freed": 0, "errored": 1}
            continue

        for entry in entries:
            if entry.key in referenced:
                continue
            last_modified = entry.last_modified
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=UTC)
            if last_modified > cutoff:
                continue
            try:
                r2_delete_object(entry.key)
            except Exception:
                logger.warning(
                    "cleanup_r2_orphans_task: failed to delete orphan '%s'",
                    entry.key,
                    exc_info=True,
                )
                continue
            deleted_here += 1
            bytes_here += entry.size

        per_prefix[prefix] = {"deleted": deleted_here, "bytes_freed": bytes_here}
        deleted_total += deleted_here
        bytes_freed_total += bytes_here

    if deleted_total:
        logger.info(
            "cleanup_r2_orphans_task: removed %d orphan(s) (%d bytes freed) "
            "across %d prefix(es)",
            deleted_total,
            bytes_freed_total,
            len(_R2_ORPHAN_CLEANUP_PREFIXES),
        )

    return {
        "status": "ok",
        "deleted": deleted_total,
        "bytes_freed": bytes_freed_total,
        "per_prefix": per_prefix,
    }


# ═══════════════════════════════════════════════════════════════════
# R2 Folder Watcher (rclone intake path)
# ═══════════════════════════════════════════════════════════════════


@shared_task(
    name="scans.watch_r2_scan_folders",
    soft_time_limit=10 * 60,
    time_limit=15 * 60,
)
def watch_r2_scan_folders_task() -> dict:
    """Discover pending R2 scan folders and notify staff of any new arrivals.

    Runs every 5 minutes (Celery beat). Scans the intake prefix in R2
    (default: ``ScanOutput/``) for folders containing new scan files.
    Ingest is intentionally **not** performed automatically — staff must
    choose the scan layout before OCR begins.

    For each folder that has not been seen since the last 24-hour window
    an in-app ``Notification`` is created for every active staff user,
    linking directly to the \"New Batch\" intake page.

    Returns:
        dict with ``ingested``, ``skipped``, ``new_folders``, and
        per-folder ``results`` keys.
    """
    from scans.scan_folder import ScanFolderWatcherService

    logger.info("R2 folder watcher: scanning for pending intake folders...")
    result = ScanFolderWatcherService.discover_and_ingest_all(auto_process=True)

    new_folders: list[dict] = result.get("new_folders", [])
    logger.info(
        "R2 folder watcher done: %d new folder(s), %d already notified.",
        len(new_folders),
        result.get("skipped", 0),
    )

    if new_folders:
        _notify_staff_of_new_scan_folders(new_folders)

    # Folder dicts carry a live Campaign model instance for the admin template
    # (see scan_folder._build_pending_folder_entry). Strip it before returning so
    # Celery's JSON result backend can encode the task return value.
    result["new_folders"] = [
        {k: v for k, v in folder.items() if k != "campaign"} for folder in new_folders
    ]
    return result


def _notify_staff_of_new_scan_folders(new_folders: list[dict]) -> None:
    """Create in-app Notification records for every active staff user.

    One notification per folder is sent to each staff member, linking to
    the scan intake page so they can ingest with one click.

    Args:
        new_folders: List of folder entry dicts from ``discover_and_ingest_all``,
            each containing ``r2_prefix``, ``file_count``, ``campaign_name``,
            ``is_valid``, and ``notification_type``.
    """
    from core.models import User
    from notifications.models import Notification

    intake_url = "/admin/scan-processing/new-batch/"

    staff_users = list(
        User.objects.filter(is_staff=True, is_active=True).values_list("id", flat=True)
    )
    if not staff_users:
        logger.warning("Folder watcher: no active staff users to notify.")
        return

    notifications: list[Notification] = []
    for folder in new_folders:
        prefix: str = folder["r2_prefix"]
        file_count: int = folder.get("file_count", 0)
        campaign_name: str = folder.get("campaign_name", prefix)
        notif_type: str = folder.get("notification_type", Notification.TYPE_INFO)
        is_valid: bool = folder.get("is_valid", False)

        if is_valid:
            title = f"New scans ready to ingest — {folder.get('appeal_code', '')} / {folder.get('payment_method', '')}"
            message = (
                f"{file_count} file(s) arrived in R2 folder '{prefix}' for "
                f"{campaign_name}. Open the intake page to choose a scan layout and start OCR."
            )
        else:
            title = "Unrecognised R2 scan folder — action required"
            message = (
                f"{file_count} file(s) arrived in R2 folder '{prefix}' but the "
                f"campaign could not be resolved ({campaign_name}). "
                f"Check the folder path or create the campaign before ingesting."
            )

        for user_id in staff_users:
            notifications.append(
                Notification(
                    user_id=user_id,
                    title=title[:200],
                    message=message,
                    notification_type=notif_type,
                    related_object_type="ScanFolder",
                    related_object_id=prefix[:255],
                    link=intake_url,
                )
            )

    if notifications:
        Notification.objects.bulk_create(notifications, ignore_conflicts=True)
        logger.info(
            "Folder watcher: created %d notification(s) for %d staff user(s) across %d new folder(s).",
            len(notifications),
            len(staff_users),
            len(new_folders),
        )
