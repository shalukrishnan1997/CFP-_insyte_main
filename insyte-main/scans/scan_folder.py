"""Folder-based scan intake service for R2-backed processing.

Each intake prefix should contain exactly one uploaded PDF representing a
single physical batch. OCR then expands the PDF into per-page virtual keys
and groups those pages into donor documents based on the selected layout.

The scanner PC only needs **rclone** — no Python, no QR decoding.  Staff
create one permanent folder per scanning job using the three-segment path
convention, and rclone mirrors it to R2 automatically.

R2 folder convention
--------------------
Path structure (all three segments are required)::

    {SCAN_R2_INTAKE_PREFIX}/{client_code}/{appeal_code}/{payment_method}/

``client_code``
    Short uppercase code set in INSYTE Client Setup (e.g. ``BRC``).

``appeal_code``
    Campaign appeal code as configured in INSYTE (e.g. ``SPRING25``).

``payment_method``
    Must exactly match one of the PAYMENT_METHOD_CHOICES values::

        card, direct_debit, cash, caf,
        cheque, postal_order, non_financial

Example scanner PC folder structure::

    C:\\ScanOutput\\BRC\\SPRING25\\cheque\\
    C:\\ScanOutput\\BRC\\SPRING25\\card\\
    C:\\ScanOutput\\RNIB\\WINTER25\\direct_debit\\

rclone mirrors to R2 (every 2 minutes, ``--min-age 30s``)::

    r2:bucket/ScanOutput/BRC/SPRING25/cheque/
    r2:bucket/ScanOutput/BRC/SPRING25/card/
    r2:bucket/ScanOutput/RNIB/WINTER25/direct_debit/

The intake page lists every file in the prefix, cross-checks against
``ScanPlaceholder.image_path`` records that already exist in the database,
and only ingests **new** files. Staff should upload one PDF per physical
batch and rely on the PDF filename as the canonical identifier used through
scan processing and QA.

Usage
-----
::

    from scans.scan_folder import ScanFolderWatcherService

    # List all pending folders with resolved scan params
    pending = ScanFolderWatcherService.list_pending_folders()

    # Ingest one folder manually
    result = ScanFolderWatcherService.ingest_folder(
        appeal_code="SPRING25",
        payment_method="cheque",
        scan_form_type="simplex_with_payment",
        r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
        user=request.user,
    )
"""

import hashlib
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# File extensions accepted as scanned donation form images
_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".pdf", ".bmp", ".webp"}
)

# Default R2 prefix for scan intake folders
_DEFAULT_INTAKE_PREFIX = "ScanOutput/"

# Maximum number of keys per SQL IN clause / OR-LIKE batch.
# Keeps individual queries fast and avoids exceeding DB parameter limits.
_DB_IN_CHUNK_SIZE = 500


def _intake_prefix() -> str:
    """Return the configured R2 scan intake root prefix.

    Returns:
        Prefix string ending with '/'.
    """
    prefix = getattr(settings, "SCAN_R2_INTAKE_PREFIX", _DEFAULT_INTAKE_PREFIX)
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _is_image_key(key: str) -> bool:
    """Return True if the R2 key is an image/PDF file.

    Args:
        key: R2 object key.

    Returns:
        True if the file extension is in the accepted set.
    """
    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return f".{suffix}" in _IMAGE_EXTENSIONS


def _already_ingested_keys(candidate_keys: list[str]) -> set[str]:
    """Return the subset of R2 keys that are already in the database.

    Queries ``ScanPlaceholder.image_path`` to find keys that were
    ingested in a previous run.

    This also checks if a "raw" PDF has been split by looking for keys
    starting with the PDF's base name in a ``split/`` subdirectory.
    Regex ``%_doc_%.pdf`` is the standard naming for split pages.

    The ``image_path__in`` SQL clause is batched into chunks of
    ``_DB_IN_CHUNK_SIZE`` to avoid exceeding query-parameter limits when
    there are many files.

    Args:
        candidate_keys: R2 keys to check.

    Returns:
        Set of keys already present in ``ScanPlaceholder``.
    """
    from scans.models import ScanPlaceholder

    if not candidate_keys:
        return set()

    # Chunked direct matches to avoid huge SQL IN clauses.
    all_ingested: set[str] = set()
    for i in range(0, len(candidate_keys), _DB_IN_CHUNK_SIZE):
        chunk = candidate_keys[i : i + _DB_IN_CHUNK_SIZE]
        all_ingested.update(
            ScanPlaceholder.objects.filter(image_path__in=chunk).values_list(
                "image_path", flat=True
            )
        )

    # For PDFs, also check if any "split" derivatives exist in the DB.
    # If ScanOutput/ABC/SPRING25/cheque/split/BATCH01_doc_001.pdf exists,
    # then ScanOutput/ABC/SPRING25/cheque/BATCH01.pdf is considered ingested.
    all_ingested.update(_ingested_pdf_keys(candidate_keys))

    return all_ingested


def _split_prefix_for_pdf_key(pdf_key: str) -> str:
    """Return the split-output prefix associated with an uploaded PDF key."""
    base_folder, filename = pdf_key.rsplit("/", 1)
    base_name = filename.rsplit(".", 1)[0]
    return f"{base_folder}/split/{base_name}_doc_"


def _matching_split_prefixes(pdf_candidates: list[str]) -> set[str]:
    """Return ingested split-page paths for candidate source PDFs.

    The OR-LIKE query is chunked into batches of ``_DB_IN_CHUNK_SIZE`` to
    prevent unbounded query parameter growth for large folders.
    """
    from django.db.models import Q

    from scans.models import ScanPlaceholder

    if not pdf_candidates:
        return set()

    result: set[str] = set()
    for i in range(0, len(pdf_candidates), _DB_IN_CHUNK_SIZE):
        chunk = pdf_candidates[i : i + _DB_IN_CHUNK_SIZE]
        query = Q()
        for pdf_key in chunk:
            query |= Q(image_path__startswith=_split_prefix_for_pdf_key(pdf_key))
        result.update(
            ScanPlaceholder.objects.filter(query)
            .values_list("image_path", flat=True)
            .distinct()
        )
    return result


def _ingested_pdf_keys(candidate_keys: list[str]) -> set[str]:
    """Return raw PDF keys that were previously ingested via split output."""
    pdf_candidates = [key for key in candidate_keys if key.lower().endswith(".pdf")]
    split_ingested_prefixes = _matching_split_prefixes(pdf_candidates)
    ingested_pdf_keys: set[str] = set()
    for pdf_key in pdf_candidates:
        split_prefix = _split_prefix_for_pdf_key(pdf_key)
        if any(path.startswith(split_prefix) for path in split_ingested_prefixes):
            ingested_pdf_keys.add(pdf_key)
    return ingested_pdf_keys


def _list_all_keys_under_prefix(prefix: str, max_keys: int = 0) -> list[str]:
    """List all R2 object keys under a given prefix with pagination.

    Paginates through all results so large folders are never silently
    truncated.  If ``max_keys`` is positive it acts as a hard upper-bound
    safety valve and a warning is logged when the limit is reached so the
    operator knows the folder exceeds a reasonable size.

    Args:
        prefix: R2 key prefix to list.
        max_keys: Maximum total keys to return.  ``0`` means unlimited.

    Returns:
        List of R2 object keys.
    """
    from core.storage_backends import get_r2_client, r2_enabled

    if not r2_enabled():
        return []

    keys: list[str] = []
    try:
        client = get_r2_client()
        paginator = client.get_paginator("list_objects_v2")
        pagination_config: dict[str, int] = {}
        if max_keys > 0:
            pagination_config["MaxItems"] = max_keys
        for page in paginator.paginate(
            Bucket=settings.R2_BUCKET_NAME,
            Prefix=prefix,
            PaginationConfig=pagination_config,
        ):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        if max_keys > 0 and len(keys) >= max_keys:
            logger.warning(
                "R2 prefix '%s' has >= %d objects; listing was capped at max_keys=%d. "
                "Some files may have been missed. Consider splitting the folder.",
                prefix,
                max_keys,
                max_keys,
            )
    except Exception:
        logger.exception("R2 list failed for prefix '%s'", prefix)
    return keys


def _new_image_keys_under_prefix(prefix: str) -> list[str]:
    """Return non-ingested image keys under the provided prefix."""
    all_keys = _list_all_keys_under_prefix(prefix)
    if not all_keys:
        return []
    all_image_keys = [key for key in all_keys if _is_image_key(key)]
    ingested = _already_ingested_keys(all_image_keys)
    return [key for key in all_image_keys if key not in ingested]


def _folder_prefix_for_key(key: str, intake_prefix: str) -> str | None:
    """Return the three-segment folder prefix for an intake object key."""
    relative = key[len(intake_prefix) :]
    parts = relative.split("/")
    if len(parts) < 4 or not parts[-1]:
        logger.debug("Key '%s' is not in a 3-segment folder — skipped.", key)
        return None
    return intake_prefix + "/".join(parts[:3]) + "/"


def _group_keys_by_folder(
    new_keys: list[str], intake_prefix: str
) -> dict[str, list[str]]:
    """Group image keys by their containing intake folder."""
    folder_map: dict[str, list[str]] = {}
    for key in new_keys:
        folder_prefix = _folder_prefix_for_key(key, intake_prefix)
        if folder_prefix is not None:
            folder_map.setdefault(folder_prefix, []).append(key)
    return folder_map


def _folder_segments(
    folder_prefix: str, intake_prefix: str
) -> tuple[str, str, str] | None:
    """Parse a folder prefix into client code, appeal code, and payment method."""
    relative = folder_prefix[len(intake_prefix) :].rstrip("/")
    parts = relative.split("/")
    if len(parts) != 3:
        logger.warning(
            "Skipping folder '%s': expected 3 path segments "
            "(client_code/appeal_code/payment_method), got %d.",
            folder_prefix,
            len(parts),
        )
        return None
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def _scan_form_type_choices(payment_method: str) -> list[dict[str, Any]]:
    """Return allowed layout choices for a payment method."""
    from scans.models import ScanBatch

    return [
        {
            "value": value,
            "label": label,
            "pages_per_donor": ScanBatch.scan_form_type_pages_per_donor(value),
        }
        for value, label in ScanBatch.allowed_scan_form_type_choices_for_payment_method(
            payment_method
        )
    ]


def _build_pending_folder_entry(
    folder_prefix: str,
    image_keys: list[str],
    intake_prefix: str,
    valid_payment_methods: set[str],
) -> dict[str, Any] | None:
    """Build a single pending-folder entry for the intake page."""
    segments = _folder_segments(folder_prefix, intake_prefix)
    if segments is None:
        return None

    client_code, appeal_code, payment_method = segments
    payment_valid = payment_method in valid_payment_methods
    if not payment_valid:
        logger.warning(
            "Skipping folder '%s': '%s' is not a valid payment_method. "
            "Valid values: %s",
            folder_prefix,
            payment_method,
            ", ".join(sorted(valid_payment_methods)),
        )

    pdf_keys = [k for k in image_keys if k.lower().endswith(".pdf")]
    non_pdf_keys = [k for k in image_keys if not k.lower().endswith(".pdf")]

    campaign = ScanFolderWatcherService._resolve_campaign(
        appeal_code=appeal_code, client_code=client_code
    )
    from campaigns.ingest_guards import campaign_scan_block_reason

    block_reason = (
        campaign_scan_block_reason(campaign) if campaign is not None else None
    )
    return {
        "client_code": client_code,
        "appeal_code": appeal_code,
        "payment_method": payment_method,
        "r2_prefix": folder_prefix,
        "file_count": len(pdf_keys),
        "non_pdf_count": len(non_pdf_keys),
        # Live Campaign instance for the admin "Pending R2 Folders" template
        # (reads folder.campaign.name / .client.name / .scan_purpose).
        # Stripped before this dict is returned from watch_r2_scan_folders_task
        # because Celery's JSON result backend can't serialize Django models.
        "campaign": campaign,
        "campaign_name": (
            f"{campaign.client.name} — {campaign.name}"
            if campaign
            else f"⚠ No campaign found for appeal code '{appeal_code}'"
        ),
        "is_valid": campaign is not None and payment_valid and block_reason is None,
        "payment_method_invalid": not payment_valid,
        "campaign_inactive_reason": block_reason,
        "scan_form_type_choices": _scan_form_type_choices(payment_method),
    }


def _validate_physical_batch_files(image_keys: list[str]) -> str | None:
    """Validate that a physical-batch file list is not empty.

    Non-PDF filtering is handled upstream in ``ingest_folder`` before
    this function is called, so only an empty list needs to be rejected.
    """
    if not image_keys:
        return "No files found to ingest."
    return None


def _build_batch_payload(scan_batch: Any) -> dict[str, Any]:
    """Return serialized info about a created scan batch."""
    return {
        "scan_batch_id": str(scan_batch.id),
        "batch_name": scan_batch.batch_name,
        "file_count": int(scan_batch.total_scans),
    }


def _new_keys_for_ingest(r2_prefix: str) -> tuple[list[str], dict[str, Any] | None]:
    """Return new image keys for an ingest request or an API response dict."""
    all_keys = _list_all_keys_under_prefix(r2_prefix)
    all_image_keys = sorted(key for key in all_keys if _is_image_key(key))
    if not all_image_keys:
        return [], {
            "status": "error",
            "error": f"No image files found under R2 prefix '{r2_prefix}'.",
        }

    ingested = _already_ingested_keys(all_image_keys)
    new_keys = [key for key in all_image_keys if key not in ingested]
    if not new_keys:
        return [], {
            "status": "ok",
            "file_count": 0,
            "total_batches": 0,
            "batches": [],
            "processing_triggered": False,
            "message": "All files in this folder have already been ingested.",
        }
    return new_keys, None


def _create_scan_batch_from_file(
    campaign: Any,
    payment_method: str,
    scan_form_type: str,
    user: Any | None,
    r2_prefix: str,
    auto_process: bool,
    image_keys: list[str],
) -> dict[str, Any]:
    """Create a single scan batch from one uploaded physical-batch PDF."""
    from scans.scan_processing import ScanProcessingService

    validation_error = _validate_physical_batch_files(image_keys)
    if validation_error:
        return {
            "status": "error",
            "error": validation_error,
        }

    logger.info(
        "Ingesting physical batch PDF from R2 '%s' (campaign: %s)",
        r2_prefix,
        campaign.name,
    )

    try:
        scan_batch = ScanProcessingService.create_scan_batch_from_r2(
            campaign_id=str(campaign.id),
            r2_keys=image_keys,
            payment_method=payment_method,
            scan_form_type=scan_form_type,
            user=user,
        )
    except ValueError as exc:
        logger.warning(
            "Invalid physical batch upload for prefix '%s': %s",
            r2_prefix,
            exc,
        )
        return {
            "status": "error",
            "error": str(exc),
        }
    except Exception:
        logger.exception("Failed to create scan batch for prefix '%s'", r2_prefix)
        return {
            "status": "error",
            "error": "Failed to create batch. Check server logs.",
        }

    if auto_process:
        from scans.tasks import process_scan_batch_task

        process_scan_batch_task.delay(str(scan_batch.id))
        logger.info(
            "OCR triggered for batch %s (%s)", scan_batch.id, scan_batch.batch_name
        )

    return {
        "status": "ok",
        "file_count": int(scan_batch.total_scans),
        "total_batches": 1,
        "batches": [_build_batch_payload(scan_batch)],
        "processing_triggered": auto_process,
    }


class ScanFolderWatcherService:
    """Discover and ingest R2 scan folders into ScanBatch records.

    Each eligible prefix should contain one uploaded PDF representing a
    single physical batch. OCR then expands that PDF into donor documents
    according to the selected scan layout.

    All methods are static — no instance state required.

    Required R2 folder structure (all three segments mandatory)::

        {SCAN_R2_INTAKE_PREFIX}/{client_code}/{appeal_code}/{payment_method}/

    Example::

        ScanOutput/BRC/SPRING25/cheque/
        ScanOutput/BRC/SPRING25/card/
        ScanOutput/RNIB/WINTER25/direct_debit/

    Folders with missing segments are silently skipped with a warning log.
    """

    @staticmethod
    def list_pending_folders() -> list[dict[str, Any]]:
        """Discover R2 scan folders that contain new (not-yet-ingested) images.

        Expects the strict 3-segment path convention::

            {intake_prefix}/{client_code}/{appeal_code}/{payment_method}/

        Any folder that does not match all three segments or uses an
        unrecognised ``payment_method`` is skipped with a warning log.

        Returns:
            List of dicts, each describing a pending folder:
                - client_code (str)
                - appeal_code (str)
                - payment_method (str)
                - r2_prefix (str): full R2 prefix with trailing ``'/'``
                - file_count (int): number of new image files
                - campaign: resolved ``Campaign`` instance or ``None``
                - campaign_name (str): display name or error message
                - is_valid (bool): ``True`` when campaign resolved and all
                  path segments are recognised
                - payment_method_invalid (bool): ``True`` if payment_method
                  not in ``PAYMENT_METHOD_CHOICES``
                - scan_form_type_choices (list[dict[str, Any]]): visible
                  batch layout choices allowed for the payment method
        """
        from scans.scan_constants import PAYMENT_METHOD_CHOICES

        intake_prefix = _intake_prefix()
        valid_payment_methods = {v for v, _ in PAYMENT_METHOD_CHOICES}
        new_keys = _new_image_keys_under_prefix(intake_prefix)
        if not new_keys:
            return []

        pending: list[dict[str, Any]] = []
        folder_map = _group_keys_by_folder(new_keys, intake_prefix)
        for folder_prefix, image_keys in folder_map.items():
            entry = _build_pending_folder_entry(
                folder_prefix,
                image_keys,
                intake_prefix,
                valid_payment_methods,
            )
            if entry is not None and entry["file_count"] > 0:
                pending.append(entry)

        # Valid folders first, then alphabetical by path
        pending.sort(key=lambda x: (not x["is_valid"], x["r2_prefix"]))
        return pending

    @staticmethod
    def _resolve_campaign(
        appeal_code: str, client_code: str | None = None
    ) -> Any | None:
        """Look up a Campaign by appeal_code and optionally client_code.

        Args:
            appeal_code: The appeal code to search for (case-insensitive).
            client_code: Optional client code to narrow the search.

        Returns:
            Campaign instance or None if not found or ambiguous.
        """
        from django.core.exceptions import MultipleObjectsReturned

        from campaigns.models import Campaign

        filters = {"appeal_code__iexact": appeal_code}
        if client_code:
            filters["client__client_code__iexact"] = client_code

        try:
            return Campaign.objects.select_related("client").get(**filters)
        except Campaign.DoesNotExist:
            logger.debug(
                "No campaign found for appeal_code='%s' (client_code='%s')",
                appeal_code,
                client_code,
            )
        except MultipleObjectsReturned:
            logger.warning(
                "Multiple campaigns found for appeal_code='%s' (client_code='%s'). "
                "Ambiguous match — skipping folder.",
                appeal_code,
                client_code,
            )
        except Exception:
            logger.exception(
                "Unexpected error resolving campaign for appeal_code='%s'", appeal_code
            )
        return None

    @staticmethod
    def ingest_folder(
        appeal_code: str,
        payment_method: str,
        scan_form_type: str,
        r2_prefix: str,
        user: Any | None = None,
        auto_process: bool = True,
    ) -> dict[str, Any]:
        """Ingest one uploaded physical-batch PDF from an R2 folder.

        Only images whose R2 key does **not** already exist in
        ``ScanPlaceholder.image_path`` are ingested.  This means calling
        this method on the same folder twice is safe — the second call
        returns ``file_count: 0`` and creates no batches.

        Steps:

        1. Resolve campaign from appeal_code.
        2. Sort and list image files under r2_prefix.
        3. Exclude keys already in DB (idempotency — no ``.done`` file needed).
        4. Ensure exactly one new PDF exists for the physical batch.
        5. Create one ScanBatch + ScanPlaceholders for that PDF.
        6. Optionally trigger OCR Celery task for the batch.

        Args:
            appeal_code: Campaign appeal code (used for the campaign lookup).
            payment_method: Payment method for all donations in this batch.
            scan_form_type: Physical document layout chosen for this batch.
            r2_prefix: Full R2 prefix for this folder, trailing slash included
                (e.g. ``'ScanOutput/BRC/SPRING25/cheque/'``).
            user: Staff user triggering the ingest (optional).
            auto_process: If True, immediately trigger OCR Celery task.

        Returns:
            dict with keys:
                - status (str): 'ok' or 'error'
                - file_count (int): number of new uploaded files ingested
                - batches (list[dict]): batch info (scan_batch_id, batch_name, file_count)
                - total_batches (int): always 1 for a valid physical batch upload
                - error (str): only present when status == 'error'
        """
        # Extract client_code from r2_prefix if available
        intake_prefix = _intake_prefix()
        client_code = None
        if r2_prefix.startswith(intake_prefix):
            segments = _folder_segments(r2_prefix, intake_prefix)
            if segments:
                client_code, _, _ = segments

        campaign = ScanFolderWatcherService._resolve_campaign(
            appeal_code=appeal_code, client_code=client_code
        )
        if campaign is None:
            return {
                "status": "error",
                "error": f"No active campaign found with appeal code '{appeal_code}' (client: {client_code or 'any'}).",
            }

        from campaigns.ingest_guards import campaign_scan_block_reason

        block_reason = campaign_scan_block_reason(campaign)
        if block_reason is not None:
            return {"status": "error", "error": block_reason}

        image_keys, early_response = _new_keys_for_ingest(r2_prefix)
        if early_response is not None:
            return early_response

        # Filter for PDFs only — non-PDFs (JPEGs, TIFFs, etc.) are silently skipped.
        pdf_keys = [key for key in image_keys if key.lower().endswith(".pdf")]
        non_pdf_keys = [key for key in image_keys if not key.lower().endswith(".pdf")]
        if non_pdf_keys:
            logger.warning(
                "Skipping %d non-PDF file(s) in '%s': %s",
                len(non_pdf_keys),
                r2_prefix,
                ", ".join(non_pdf_keys),
            )
        if not pdf_keys:
            return {
                "status": "error",
                "error": f"No PDF files found under R2 prefix '{r2_prefix}'.",
            }

        # Ingest each PDF as its own scan batch
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        for pdf_key in pdf_keys:
            res = _create_scan_batch_from_file(
                campaign=campaign,
                payment_method=payment_method,
                scan_form_type=scan_form_type,
                user=user,
                r2_prefix=r2_prefix,
                auto_process=auto_process,
                image_keys=[pdf_key],
            )
            if res["status"] == "ok":
                results.extend(res.get("batches", []))
            else:
                errors.append(f"{pdf_key}: {res.get('error', 'Unknown error')}")

        if not results and errors:
            return {
                "status": "error",
                "error": "; ".join(errors),
            }

        return {
            "status": "ok",
            "file_count": sum(int(b["file_count"]) for b in results),
            "total_batches": len(results),
            "batches": results,
            "processing_triggered": auto_process,
            "message": f"Successfully created {len(results)} batch(es)."
            if not errors
            else f"Created {len(results)} batch(es). Errors: {'; '.join(errors)}",
        }

    # Cache TTL used to suppress repeat notifications for the same folder.
    # After 24 hours a persistent folder is re-notified so it doesn't get
    # silently forgotten if staff dismissed the first alert.
    _FOLDER_SEEN_CACHE_TTL = 86400  # seconds

    @staticmethod
    def _folder_seen_cache_key(r2_prefix: str) -> str:
        """Return a deterministic cache key for a given R2 prefix."""
        digest = hashlib.md5(r2_prefix.encode(), usedforsecurity=False).hexdigest()
        return f"scan_r2_folder_seen:{digest}"

    @staticmethod
    def discover_and_ingest_all(auto_process: bool = True) -> dict[str, Any]:
        """Discover pending R2 folders and identify the ones never notified before.

        Ingest is intentionally **not** performed here — batch layout must be
        chosen explicitly by staff at ingest time.  Instead this method:

        1. Lists all currently pending folders via ``list_pending_folders``.
        2. Uses Django's cache to distinguish folders that staff have already
           been alerted about (within the last 24 hours) from truly new ones.
        3. Marks new folders as "seen" in the cache so subsequent watcher
           runs do not re-notify within the TTL window.

        The caller (``watch_r2_scan_folders_task``) is responsible for
        creating in-app ``Notification`` records for each new folder entry
        returned here.

        Args:
            auto_process: Unused — kept for call-site compatibility.

        Returns:
            dict with:
                - ``ingested`` (int): always 0 (ingest is manual).
                - ``skipped`` (int): total pending folders already known.
                - ``new_folders`` (list[dict]): folder entries seen for the
                  first time this run; each entry is a ``list_pending_folders``
                  dict enriched with a ``notification_type`` key
                  (``"warning"`` for unresolvable campaigns, ``"info"`` otherwise).
                - ``results`` (list[dict]): per-folder status summary.
        """
        from django.core.cache import cache

        pending = ScanFolderWatcherService.list_pending_folders()

        new_folders: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        skipped = 0

        for folder in pending:
            prefix = folder["r2_prefix"]
            cache_key = ScanFolderWatcherService._folder_seen_cache_key(prefix)

            if cache.get(cache_key):
                # Already notified recently — just count as skipped.
                skipped += 1
                results.append({"r2_prefix": prefix, "status": "already_notified"})
                logger.debug("Folder watcher: '%s' already notified, skipping.", prefix)
                continue

            # First time this folder has been seen — mark it and queue notification.
            cache.set(
                cache_key, True, timeout=ScanFolderWatcherService._FOLDER_SEEN_CACHE_TTL
            )

            notif_type = "warning" if not folder["is_valid"] else "info"
            new_folders.append({**folder, "notification_type": notif_type})
            results.append({"r2_prefix": prefix, "status": "new"})
            logger.info(
                "Folder watcher: new folder '%s' (%d file(s)) — notification queued.",
                prefix,
                folder["file_count"],
            )

        logger.info(
            "Folder watcher run complete: 0 ingested, %d new, %d already notified.",
            len(new_folders),
            skipped,
        )
        return {
            "ingested": 0,
            "skipped": skipped,
            "new_folders": new_folders,
            "results": results,
        }
