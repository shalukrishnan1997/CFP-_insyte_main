"""Manual scan redaction helpers (PCI-oriented gates and storage writes)."""

import logging
import uuid
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import sentry_sdk
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from core.models import User
    from scans.models import ScanPlaceholder

logger = logging.getLogger(__name__)


def placeholder_payment_method(placeholder: ScanPlaceholder) -> str:
    """Return the payment method tied to *placeholder*.

    Reads from the linked ``Donation`` once QA captures the placeholder, and
    falls back to ``extracted_data['payment_method']`` (set by OCR) before
    capture. Returns ``""`` if the method has not yet been determined.
    """
    donation = getattr(placeholder, "donation", None)
    if donation is not None:
        method = getattr(donation, "payment_method", "") or ""
        if method:
            return method
    extracted = placeholder.extracted_data or {}
    return str(extracted.get("payment_method", "") or "")


# Prefix where redaction bytes are first written before being atomically
# swapped into the final destination key. The orphan-cleanup task watches
# this prefix and deletes anything older than 24h that no placeholder
# references.
REDACTED_TMP_PREFIX = "redacted-tmp/"


def _temp_redaction_key(swap_id: str, destination_key: str) -> str:
    """Build a unique temp R2 key under ``redacted-tmp/<swap-uuid>/``.

    The swap UUID gives every retry a fresh namespace so a partial blob from
    an earlier failed attempt is never read or overwritten by the next call.
    """
    return f"{REDACTED_TMP_PREFIX}{swap_id}/{destination_key}"


def manual_redaction_required_for(payment_method: str) -> bool:
    """Return True when *payment_method* requires manual redaction before approval.

    Reads from the ``RedactionSettings`` singleton. Unknown methods (or an
    empty string) default to *not required*.
    """
    from scans.models import RedactionSettings

    return RedactionSettings.get_settings().requires_redaction(payment_method)


def can_view_unredacted_pending_scan(
    user: Any | None, *, allow_qa_pending_redaction: bool = False
) -> bool:
    """Return True if *user* may view originals while redaction is incomplete."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_staff", False):
        return False
    if not allow_qa_pending_redaction:
        return False
    if getattr(settings, "STRICT_UNREDACTED_SCAN_VIEW", False):
        # App-label-agnostic: matches "core.view_unredacted_scan" today and
        # "scans.view_unredacted_scan" once ScanPlaceholder moves in Phase 2.
        suffix = ".view_unredacted_scan"
        return any(p.endswith(suffix) for p in user.get_all_permissions())

    from custom_admin.views.qa_utils import has_qa_access

    return has_qa_access(user)


def scan_urls_hidden_for_user(
    placeholder: ScanPlaceholder,
    user: Any | None,
    *,
    allow_pending_redaction: bool = False,
) -> bool:
    """Return True when scan image URLs must be hidden for *user*.

    DEFERRED placeholders (coords saved, blackout pending charge) are treated
    the same as PENDING — the underlying R2 image is still readable, so the
    same permission gate applies.
    """
    if placeholder.redaction_status == placeholder.REDACTION_COMPLETED:
        return False
    payment_method = placeholder_payment_method(placeholder)
    if not manual_redaction_required_for(payment_method):
        return False
    return not can_view_unredacted_pending_scan(
        user, allow_qa_pending_redaction=allow_pending_redaction
    )


def redaction_blocks_processing(placeholder: ScanPlaceholder) -> bool:
    """Return True when OCR must not run for this placeholder."""
    if placeholder.redaction_status != placeholder.REDACTION_BLOCKED:
        return False
    payment_method = placeholder_payment_method(placeholder)
    return manual_redaction_required_for(payment_method)


def expected_redaction_page_count(placeholder: ScanPlaceholder) -> int:
    """Number of pages/files staff must supply when uploading redacted copies."""
    keys = placeholder.original_page_keys or []
    if keys:
        return len(keys)
    return 1 if placeholder.image_path else 0


def _redacted_r2_storage_key(
    original_key: str, placeholder_id: str, index: int, upload_filename: str
) -> str:
    """Build an R2 key for a redacted page next to the original."""
    base_key = original_key.split("#", 1)[0]
    path = PurePosixPath(base_key)
    parent = path.parent
    stem = path.stem or "page"
    upload_suffix = PurePosixPath(upload_filename).suffix
    suffix = upload_suffix or path.suffix or ".bin"
    short = placeholder_id.replace("-", "")[:12]
    filename = f"{stem}_redacted_{short}_{index}{suffix}"
    if str(parent) == ".":
        return filename
    return f"{parent.as_posix()}/{filename}"


_DEFAULT_REDACTION_MAX_SOURCE_BYTES = 50 * 1024 * 1024


def _max_redaction_source_bytes() -> int:
    """Per-page R2 source size cap before server-side rasterization is rejected."""
    return int(
        getattr(
            settings,
            "REDACTION_MAX_SOURCE_BYTES",
            _DEFAULT_REDACTION_MAX_SOURCE_BYTES,
        )
    )


def _atomic_upload_redacted_page(
    body: bytes, destination_key: str, content_type: str, swap_id: str
) -> None:
    """Write *body* to a temp R2 key, then server-side swap into *destination_key*.

    Three-step pattern that guarantees the final key never holds a
    partially-written blob:

    1. ``put_object`` to ``redacted-tmp/{swap_id}/{destination_key}``. If
       this raises, the final key is untouched.
    2. ``copy_object`` from the temp key to *destination_key*. R2 performs
       this server-side — there's no partial-write window for the bytes.
    3. ``delete_object`` on the temp key. If this fails, the temp blob is
       left for the periodic orphan-cleanup task to sweep.

    Args:
        body: Redacted page bytes to upload.
        destination_key: Final R2 key the placeholder will reference.
        content_type: MIME type to record on the object.
        swap_id: Unique identifier for this swap attempt; isolates retries
            so concurrent attempts cannot collide on the temp prefix.
    """
    from core.storage_backends import r2_copy_object, r2_delete_object, r2_put_object

    temp_key = _temp_redaction_key(swap_id, destination_key)
    r2_put_object(temp_key, body, content_type=content_type)
    try:
        r2_copy_object(temp_key, destination_key)
    except Exception:
        # Final key is still untouched; clean up the temp blob best-effort.
        try:
            r2_delete_object(temp_key)
        except Exception:
            logger.warning(
                "Failed to clean temp redaction blob '%s' after copy failure",
                temp_key,
                exc_info=True,
            )
        raise

    try:
        r2_delete_object(temp_key)
    except Exception:
        # The orphan-cleanup task will eventually remove the orphaned
        # temp blob — don't fail the redaction over a stray delete.
        logger.warning(
            "Failed to delete temp redaction blob '%s' (orphan cleanup will retry)",
            temp_key,
            exc_info=True,
        )


def _cleanup_orphaned_redaction_keys(
    placeholder_id: str, uploaded_keys: list[str]
) -> None:
    """Best-effort delete redacted blobs already uploaded before a mid-batch failure.

    Once :func:`_atomic_upload_redacted_page` returns, the bytes have
    already been swapped out of ``redacted-tmp/`` into the *final*
    destination key. The periodic R2 orphan-cleanup task only sweeps the
    ``redacted-tmp/`` prefix, so it will never see these keys — without an
    explicit cleanup pass storage grows unboundedly on every redaction
    retry.

    Each delete is best-effort: per-key exceptions are swallowed and
    logged so the surrounding redaction failure (the real error) is the
    one that propagates back to the caller.

    Args:
        placeholder_id: Stringified placeholder UUID, for log context.
        uploaded_keys: Final R2 destination keys that were successfully
            uploaded before the rollback fired.
    """
    if not uploaded_keys:
        return

    from core.storage_backends import r2_delete_object

    for orphan_key in uploaded_keys:
        try:
            r2_delete_object(orphan_key)
        except Exception:
            logger.warning(
                "Failed to delete orphaned redacted R2 object '%s' "
                "during rollback for placeholder %s; storage may need "
                "manual cleanup",
                orphan_key,
                placeholder_id,
                exc_info=True,
            )


def _mark_placeholder_redaction_blocked(
    placeholder: ScanPlaceholder, error_message: str
) -> None:
    """Persist a redaction failure without losing the previous (unredacted) state.

    ``page_keys`` / ``image_path`` are deliberately *not* touched: QA must
    still be able to retry from the same source pages, and we must never
    leave QA pointing at a partially-uploaded redacted blob.

    The save is wrapped in ``transaction.atomic()`` so a partial DB write
    rolls back rather than leaving the placeholder in
    ``REDACTION_IN_PROGRESS`` while the original upload exception masks the
    underlying DB error. After the save commits we report the failure to
    Sentry (no-op when Sentry is not configured) so operators can discover
    blocked placeholders without tailing Celery logs.
    """
    with transaction.atomic():
        placeholder.redaction_status = placeholder.REDACTION_BLOCKED
        placeholder.redaction_error = error_message[:2000]
        placeholder.save(
            update_fields=[
                "redaction_status",
                "redaction_error",
                "updated_at",
            ]
        )

    # Capture the *current* exception (we're called from inside an
    # ``except`` block) so operators get a Sentry alert with the failure
    # traceback. ``capture_exception()`` with no args picks up
    # ``sys.exc_info()``; if there isn't an active exception it falls back
    # to recording the message we already wrote to ``redaction_error``.
    try:
        sentry_sdk.capture_exception()
    except Exception:
        # Never let a Sentry transport blip mask the underlying upload
        # failure that the caller is about to re-raise.
        logger.warning(
            "Sentry capture_exception failed for blocked placeholder %s",
            placeholder.id,
            exc_info=True,
        )


def _finalize_placeholder_redaction(
    placeholder: ScanPlaceholder,
    new_keys: list[str],
    original_keys: list[str],
    *,
    user: User | None,
    redaction_notes: str,
) -> None:
    """Persist redacted-key state on a placeholder and purge originals from R2.

    Shared between the multipart-upload and server-side-coordinate redaction
    paths so the audit field set and R2 cleanup loop stay in lockstep.

    The placeholder DB writes are wrapped in ``transaction.atomic()`` so a
    partial DB write rolls back rather than leaving the placeholder in a
    half-updated state.
    """
    if not new_keys:
        raise ValueError(
            "_finalize_placeholder_redaction requires at least one new key"
        )

    from core.storage_backends import r2_delete_object, r2_public_url

    with transaction.atomic():
        placeholder.page_keys = new_keys
        placeholder.image_path = new_keys[0]
        placeholder.image_url = r2_public_url(new_keys[0])
        placeholder.redaction_status = placeholder.REDACTION_COMPLETED
        placeholder.redaction_completed_at = timezone.now()
        placeholder.redacted_by = user if user and user.is_authenticated else None
        placeholder.redaction_notes = redaction_notes.strip()
        placeholder.redaction_error = ""
        placeholder.save(
            update_fields=[
                "page_keys",
                "image_path",
                "image_url",
                "redaction_status",
                "redaction_completed_at",
                "redacted_by",
                "redaction_notes",
                "redaction_error",
                "updated_at",
            ]
        )

    placeholder_id = str(placeholder.id)
    new_key_set = set(new_keys)
    for original_key in original_keys:
        if original_key in new_key_set:
            continue
        try:
            r2_delete_object(original_key)
        except Exception:
            logger.warning(
                "Failed to delete original R2 object '%s' after redaction of placeholder %s",
                original_key,
                placeholder_id,
                exc_info=True,
            )


def save_uploaded_redacted_pages(
    *,
    placeholder: ScanPlaceholder,
    user: User | None,
    files: Sequence[UploadedFile],
    notes: str = "",
) -> list[str]:
    """Public entry point for staff-uploaded redaction pages.

    Writes a per-attempt sentinel blob to R2 first to (a) smoke-test
    storage connectivity and (b) provide a single failure surface so any
    transient ``PutObject`` error is caught before the final destination
    keys are touched. On any storage failure the placeholder is flipped
    to ``REDACTION_BLOCKED`` with ``redaction_error`` populated and
    ``page_keys`` is left pointing at the previous (unredacted) state so
    the operator can retry without losing the source document. The
    underlying upload then delegates to
    :func:`replace_placeholder_with_redacted_uploads`, which performs the
    atomic temp-key swap per page.

    Args:
        placeholder: Scan placeholder whose pages are being redacted.
        user: Staff user performing the redaction (for audit trail).
        files: One :class:`UploadedFile` per original page.
        notes: Optional operator note recorded on the placeholder.

    Returns:
        The list of new R2 keys (one per page) once the redaction
        finalises successfully.

    Raises:
        Exception: Any exception raised by R2 or the underlying upload
            flow is re-raised after the placeholder has been marked
            ``REDACTION_BLOCKED``.
    """
    from core.storage_backends import r2_delete_object, r2_put_object

    swap_id = uuid.uuid4().hex
    sentinel_key = f"{REDACTED_TMP_PREFIX}{swap_id}/.start"
    try:
        r2_put_object(sentinel_key, b"", content_type="text/plain")
    except Exception as exc:
        logger.exception(
            "Redaction sentinel write failed for placeholder %s", placeholder.id
        )
        _mark_placeholder_redaction_blocked(
            placeholder,
            f"Storage unavailable: {exc.__class__.__name__}: {exc}",
        )
        raise

    try:
        return replace_placeholder_with_redacted_uploads(
            placeholder, list(files), user=user, redaction_notes=notes
        )
    finally:
        # Best-effort sentinel cleanup; the orphan-cleanup task will
        # eventually sweep any blob left under redacted-tmp/.
        try:
            r2_delete_object(sentinel_key)
        except Exception:
            logger.warning(
                "Failed to delete redaction sentinel '%s' (orphan cleanup will retry)",
                sentinel_key,
                exc_info=True,
            )


def replace_placeholder_with_redacted_uploads(
    placeholder: ScanPlaceholder,
    uploads: list[UploadedFile],
    *,
    user: User | None = None,
    redaction_notes: str = "",
) -> list[str]:
    """Replace a placeholder's stored pages with redacted copies.

    Uses an atomic temp-key swap (see :func:`_atomic_upload_redacted_page`)
    so a mid-upload failure never leaves a partial blob at the final key
    that QA might serve. On any failure the placeholder is marked
    ``REDACTION_BLOCKED`` with ``redaction_error`` populated, and
    ``page_keys`` is left pointing at the previous (unredacted) state so
    the operator can retry.
    """
    from core.storage_backends import r2_enabled

    if not r2_enabled():
        raise ValueError("R2 storage is not configured.")

    expected = expected_redaction_page_count(placeholder)
    if expected < 1:
        raise ValueError("This placeholder has no recorded source pages to replace.")
    if len(uploads) != expected:
        raise ValueError(
            f"Upload exactly {expected} file(s) (one per original page); "
            f"received {len(uploads)}."
        )

    original_keys = [key for key in (placeholder.original_page_keys or []) if key]
    if not original_keys and placeholder.image_path:
        original_keys = [placeholder.image_path]
    if len(original_keys) != expected:
        raise ValueError(
            "Original page key list does not match the expected page count; "
            "contact support."
        )

    placeholder_id = str(placeholder.id)
    swap_id = uuid.uuid4().hex
    new_keys: list[str] = []
    try:
        for index, uploaded in enumerate(uploads):
            raw = uploaded.read()
            if not raw:
                raise ValueError("Empty file in upload.")
            destination_key = _redacted_r2_storage_key(
                original_keys[index], placeholder_id, index, uploaded.name or ""
            )
            content_type = uploaded.content_type or "application/octet-stream"
            _atomic_upload_redacted_page(raw, destination_key, content_type, swap_id)
            new_keys.append(destination_key)
    except ValueError:
        # User-input errors (empty file, etc.) — surface to the form layer
        # without flipping the placeholder into BLOCKED, since the operator
        # can simply re-upload.
        raise
    except Exception as exc:
        logger.exception("Redacted upload failed for placeholder %s", placeholder_id)
        # Delete any redacted blobs already swapped into their final
        # destination keys before the failure — they sit *outside*
        # ``redacted-tmp/`` so the periodic orphan-cleanup task won't
        # find them.
        _cleanup_orphaned_redaction_keys(placeholder_id, new_keys)
        _mark_placeholder_redaction_blocked(
            placeholder, f"Upload failed: {exc.__class__.__name__}: {exc}"
        )
        raise

    _finalize_placeholder_redaction(
        placeholder,
        new_keys,
        original_keys,
        user=user,
        redaction_notes=redaction_notes,
    )
    return new_keys


_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def _content_type_from_key(key: str) -> str:
    """Infer a MIME type from an R2 key suffix, defaulting to octet-stream."""
    base_key = key.split("#", 1)[0]
    suffix = PurePosixPath(base_key).suffix.lower()
    return _CONTENT_TYPE_BY_SUFFIX.get(suffix, "application/octet-stream")


def replace_placeholder_with_server_redacted_coords(
    placeholder: ScanPlaceholder,
    page_rects: list[list[dict[str, float]]],
    *,
    user: User | None = None,
    redaction_notes: str = "",
) -> list[str]:
    """Bake server-side blackout rectangles onto a placeholder's pages.

    For each page, fetches the original from R2, applies the operator's
    rectangles via :func:`scans.scan_redaction_renderer.apply_redactions`,
    uploads the redacted PNG to a sibling key, and deletes the original. The
    ``original_page_keys`` snapshot on the placeholder is left intact for audit.

    Args:
        placeholder: The scan placeholder whose pages are being redacted.
        page_rects: Outer index aligns positionally with the placeholder's
            ``original_page_keys``; each inner list holds the 0..1-normalized
            blackout rectangles for that page.
        user: Staff user performing the redaction.
        redaction_notes: Optional operator note stored on the placeholder.

    Returns:
        The list of new R2 keys (one per page) for the redacted PNGs.

    Raises:
        ValueError: When R2 isn't configured, page-rect count mismatches the
            original page count, or the renderer rejects a rect.
    """
    from core.storage_backends import r2_enabled, r2_get_object
    from scans.scan_redaction_renderer import apply_redactions

    if not r2_enabled():
        raise ValueError("R2 storage is not configured.")

    expected = expected_redaction_page_count(placeholder)
    if expected < 1:
        raise ValueError("This placeholder has no recorded source pages to replace.")
    if len(page_rects) != expected:
        raise ValueError(
            f"Provide rectangles for exactly {expected} page(s); "
            f"received {len(page_rects)}."
        )

    original_keys = [key for key in (placeholder.original_page_keys or []) if key]
    if not original_keys and placeholder.image_path:
        original_keys = [placeholder.image_path]
    if len(original_keys) != expected:
        raise ValueError(
            "Original page key list does not match the expected page count; "
            "contact support."
        )

    placeholder_id = str(placeholder.id)
    max_source_bytes = _max_redaction_source_bytes()
    swap_id = uuid.uuid4().hex
    new_keys: list[str] = []
    try:
        for index, rects in enumerate(page_rects):
            original_key = original_keys[index]
            source_bytes = r2_get_object(original_key)
            if not source_bytes:
                raise ValueError(f"Original page {index + 1} is empty in R2.")
            if len(source_bytes) > max_source_bytes:
                raise ValueError(
                    f"Original page {index + 1} is {len(source_bytes)} bytes, "
                    f"exceeds the {max_source_bytes}-byte server-redaction cap. "
                    f"Increase REDACTION_MAX_SOURCE_BYTES or shrink the source."
                )
            content_type = _content_type_from_key(original_key)
            redacted_bytes, _ = apply_redactions(source_bytes, content_type, rects)
            destination_key = _redacted_r2_storage_key(
                original_key, placeholder_id, index, "page.png"
            )
            _atomic_upload_redacted_page(
                redacted_bytes, destination_key, "image/png", swap_id
            )
            new_keys.append(destination_key)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception(
            "Server-side redaction failed for placeholder %s", placeholder_id
        )
        # Delete any redacted blobs already swapped into their final
        # destination keys before the failure — they sit *outside*
        # ``redacted-tmp/`` so the periodic orphan-cleanup task won't
        # find them.
        _cleanup_orphaned_redaction_keys(placeholder_id, new_keys)
        _mark_placeholder_redaction_blocked(
            placeholder,
            f"Server-side redaction failed: {exc.__class__.__name__}: {exc}",
        )
        raise

    _finalize_placeholder_redaction(
        placeholder,
        new_keys,
        original_keys,
        user=user,
        redaction_notes=redaction_notes,
    )
    return new_keys


def _empty_page_rects(page_count: int) -> list[list[dict[str, float]]]:
    """Return a coords payload with *page_count* empty rect lists.

    Used as a placeholder when only one of the two redaction layers needs
    to fire (e.g. a non-card method has no CVV box).
    """
    return [[] for _ in range(page_count)]


def save_deferred_redaction_coords(
    placeholder: ScanPlaceholder,
    *,
    cvv_page_rects: list[list[dict[str, float]]],
    post_charge_page_rects: list[list[dict[str, float]]],
    user: User | None,
    redaction_notes: str = "",
) -> None:
    """Persist QA-authored redaction rectangles without touching R2.

    The redaction is split into two layers with different timing:

    * **CVV layer** — applied immediately on the next Stripe authorization
      attempt (success *or* failure) by :func:`apply_cvv_redaction`. Required
      for card payment methods to satisfy PCI DSS Requirement 3.2 (sensitive
      authentication data must not be stored after authorization).
    * **Post-charge layer** — applied once the donation is settled (charge
      succeeded, operator rejected, or retention TTL exceeded) by
      :func:`apply_deferred_redaction`. Stays readable across declined
      retries so operators can re-read the PAN with a fresh card.

    Args:
        placeholder: ScanPlaceholder being redacted.
        cvv_page_rects: Per-page CVV-only rectangles. May contain empty
            inner lists when no CVV is present (e.g. non-card methods).
        post_charge_page_rects: Per-page post-charge rectangles (PAN,
            expiry, signature, etc.).
        user: Staff user who saved the coords.
        redaction_notes: Optional operator note.

    Raises:
        ValueError: If either coord set's page count does not match the
            placeholder's recorded source pages, or both sets are empty.
    """
    expected = expected_redaction_page_count(placeholder)
    if expected < 1:
        raise ValueError("This placeholder has no recorded source pages to redact.")
    if len(cvv_page_rects) != expected:
        raise ValueError(
            f"Provide CVV rectangles for exactly {expected} page(s); "
            f"received {len(cvv_page_rects)}."
        )
    if len(post_charge_page_rects) != expected:
        raise ValueError(
            f"Provide post-charge rectangles for exactly {expected} page(s); "
            f"received {len(post_charge_page_rects)}."
        )
    has_any = any(rects for rects in cvv_page_rects) or any(
        rects for rects in post_charge_page_rects
    )
    if not has_any:
        raise ValueError(
            "At least one redaction rectangle is required across CVV and "
            "post-charge layers."
        )

    with transaction.atomic():
        placeholder.redaction_coords_cvv = cvv_page_rects
        placeholder.redaction_coords_post_charge = post_charge_page_rects
        placeholder.redaction_status = placeholder.REDACTION_CVV_PENDING
        placeholder.redaction_notes = redaction_notes.strip()
        placeholder.redaction_error = ""
        placeholder.redacted_by = user if user and user.is_authenticated else None
        placeholder.save(
            update_fields=[
                "redaction_coords_cvv",
                "redaction_coords_post_charge",
                "redaction_status",
                "redaction_notes",
                "redaction_error",
                "redacted_by",
                "updated_at",
            ]
        )


def _has_any_rects(page_rects: list[list[dict[str, float]]] | None) -> bool:
    """Return True when *page_rects* contains at least one non-empty rect list."""
    if not page_rects:
        return False
    return any(rects for rects in page_rects)


def apply_cvv_redaction(placeholder: ScanPlaceholder) -> bool:
    """Apply the CVV-only blackout layer to a placeholder's R2 pages.

    Fires immediately on every Stripe ``PaymentIntent.create()`` return
    (success *or* failure) so CVV/CVC is removed from the scanned image at
    the moment of authorization, per PCI DSS Requirement 3.2.

    Idempotent: a no-op when the placeholder is already past
    ``REDACTION_CVV_PENDING`` (CVV pass already ran) or when there are no
    CVV coords saved (e.g. a non-card method that only has post-charge
    rectangles).

    Concurrency: takes a row-level lock on the placeholder so a concurrent
    :func:`apply_deferred_redaction` cannot race against this pass.

    Args:
        placeholder: ScanPlaceholder to redact.

    Returns:
        True if the blackout was applied; False if the call was a no-op.
    """
    from django.db import connection as _conn

    from scans.models import ScanPlaceholder as _SP

    placeholder_id = placeholder.id
    with transaction.atomic():
        qs = _SP.objects.filter(pk=placeholder_id)
        # SQLite (dev/test) silently no-ops select_for_update; only Postgres
        # actually serialises here. Either way we re-read inside the txn so
        # we observe the latest state.
        if _conn.vendor != "sqlite":
            qs = qs.select_for_update()
        locked = qs.first()
        if locked is None:
            return False
        if locked.redaction_status not in (
            _SP.REDACTION_PENDING,
            _SP.REDACTION_CVV_PENDING,
            _SP.REDACTION_BLOCKED,
        ):
            # CVV pass already ran (DEFERRED) or fully redacted (COMPLETED).
            return False
        cvv_rects = locked.redaction_coords_cvv or []
        if not _has_any_rects(cvv_rects):
            # No CVV rectangles saved — promote to DEFERRED so the
            # post-charge task can run cleanly without re-checking CVV.
            locked.redaction_status = _SP.REDACTION_DEFERRED
            locked.cvv_redacted_at = timezone.now()
            locked.save(update_fields=["redaction_status", "cvv_redacted_at"])
            return False
        # Apply the CVV layer to R2. The renderer keeps the existing
        # original_page_keys snapshot intact so the post-charge pass can
        # read from the (now CVV-redacted) page bytes later.
        replace_placeholder_with_server_redacted_coords(
            locked,
            cvv_rects,
            user=locked.redacted_by,
            redaction_notes=locked.redaction_notes,
        )
        # ``replace_placeholder_with_server_redacted_coords`` flips status to
        # COMPLETED on success — we want DEFERRED instead because the
        # post-charge layer is still pending. Re-read after the swap and
        # adjust.
        locked.refresh_from_db()
        locked.redaction_status = _SP.REDACTION_DEFERRED
        locked.cvv_redacted_at = timezone.now()
        locked.redaction_completed_at = None
        locked.save(
            update_fields=[
                "redaction_status",
                "cvv_redacted_at",
                "redaction_completed_at",
            ]
        )
        # Once the CVV layer has been applied, the new R2 keys ARE the
        # current source for any subsequent redaction passes. Replace
        # original_page_keys so the post-charge pass operates on the
        # CVV-redacted bytes (avoiding "look up an already-deleted blob").
        new_keys = list(locked.page_keys or [])
        if new_keys:
            locked.original_page_keys = new_keys
            locked.save(update_fields=["original_page_keys"])
    return True


def apply_deferred_redaction(placeholder: ScanPlaceholder) -> bool:
    """Apply the post-charge blackout layer to a placeholder's R2 pages.

    Fires after the charge has stuck (Stripe sync path or
    ``payment_intent.succeeded`` webhook), after the operator rejects the
    donation, or after the retention TTL expires.

    Safety net: if the CVV pass has not yet run for any reason
    (``REDACTION_CVV_PENDING``), apply CVV first, then the post-charge
    layer. Both passes happen inside the same row lock so an interleaved
    CVV-only task can't double-apply.

    Idempotent: a no-op once the placeholder is ``REDACTION_COMPLETED``.

    Args:
        placeholder: ScanPlaceholder to redact.

    Returns:
        True if any blackout was applied; False if the call was a no-op.
    """
    from django.db import connection as _conn

    from scans.models import ScanPlaceholder as _SP

    placeholder_id = placeholder.id
    applied = False
    with transaction.atomic():
        qs = _SP.objects.filter(pk=placeholder_id)
        if _conn.vendor != "sqlite":
            qs = qs.select_for_update()
        locked = qs.first()
        if locked is None:
            return False
        if locked.redaction_status == _SP.REDACTION_COMPLETED:
            return False
        # Safety net: CVV pass missed → run it first inside this lock.
        if locked.redaction_status in (
            _SP.REDACTION_PENDING,
            _SP.REDACTION_CVV_PENDING,
        ):
            cvv_rects = locked.redaction_coords_cvv or []
            if _has_any_rects(cvv_rects):
                replace_placeholder_with_server_redacted_coords(
                    locked,
                    cvv_rects,
                    user=locked.redacted_by,
                    redaction_notes=locked.redaction_notes,
                )
                locked.refresh_from_db()
                # Promote source keys so the post-charge pass reads from
                # the CVV-redacted bytes.
                new_keys = list(locked.page_keys or [])
                if new_keys:
                    locked.original_page_keys = new_keys
                applied = True
            locked.cvv_redacted_at = timezone.now()
            locked.redaction_status = _SP.REDACTION_DEFERRED
            locked.redaction_completed_at = None
            locked.save(
                update_fields=[
                    "cvv_redacted_at",
                    "redaction_status",
                    "redaction_completed_at",
                    "original_page_keys",
                ]
            )

        post_rects = locked.redaction_coords_post_charge or []
        if _has_any_rects(post_rects):
            replace_placeholder_with_server_redacted_coords(
                locked,
                post_rects,
                user=locked.redacted_by,
                redaction_notes=locked.redaction_notes,
            )
            applied = True
        else:
            # No post-charge rectangles to apply — mark complete so the task
            # is fully idempotent (avoids re-running on duplicate webhook
            # deliveries).
            locked.redaction_status = _SP.REDACTION_COMPLETED
            locked.redaction_completed_at = timezone.now()
            locked.save(update_fields=["redaction_status", "redaction_completed_at"])
    return applied


def placeholder_request_key_allowed(
    placeholder: ScanPlaceholder, request_key: str
) -> bool:
    """Return True if *request_key* is one of the placeholder's document keys."""
    from scans.scan_processing_r2 import parse_virtual_pdf_page_key

    keys: list[str] = []
    for k in placeholder.page_keys or []:
        if k:
            keys.append(k)
    if placeholder.image_path:
        keys.append(placeholder.image_path)
    if request_key in keys:
        return True
    src, page_no = parse_virtual_pdf_page_key(request_key)
    return bool(page_no is not None and src in keys)
