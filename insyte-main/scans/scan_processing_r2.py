"""R2 and batch-creation helpers for scan processing."""

import io
import logging
import random
import string
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_PDF_PAGE_KEY_SEPARATOR = "::pdf_page::"


def parse_virtual_pdf_page_key(r2_key: str) -> tuple[str, int | None]:
    """Parse a virtual page key into source key and page number."""
    if _PDF_PAGE_KEY_SEPARATOR not in r2_key:
        return r2_key, None

    source_key, _, page_text = r2_key.rpartition(_PDF_PAGE_KEY_SEPARATOR)
    try:
        page_number = int(page_text)
    except ValueError:
        logger.warning("Invalid virtual PDF page key '%s'", r2_key)
        return r2_key, None

    if page_number < 1:
        logger.warning("Virtual PDF page must be >=1 for key '%s'", r2_key)
        return r2_key, None

    return source_key, page_number


def count_pdf_pages(pdf_bytes: bytes) -> int | None:
    """Count pages in a PDF byte stream."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        logger.exception("Failed to count pages in PDF during scan key expansion")
        return None


def _split_pdf_pages_to_r2(
    source_key: str,
    pdf_bytes: bytes,
    page_count: int,
) -> list[str]:
    """Split a multi-page PDF and upload individual page objects to R2.

    Scanned PDFs are treated as image-only intake sources. Each page must be
    extracted to a PNG object so QR decoding and OCR operate on raster image
    bytes directly. This intentionally removes PDF as an intermediate split
    format; PDF output remains available separately for donor review/export.

    Args:
        source_key: R2 key of the multi-page source PDF.
        pdf_bytes: Raw bytes of the source PDF (already downloaded).
        page_count: Number of pages in the PDF.

    Returns:
        Ordered list of R2 keys for the uploaded split pages
        (``split/…_doc_0001.png``, ``split/…_doc_0002.png``, …).
    """
    from django.conf import settings
    from pypdf import PdfReader

    from core.storage_backends import get_r2_client

    bucket: str = settings.R2_BUCKET_NAME
    r2_client = get_r2_client()

    # Build the split prefix matching scan_folder._split_prefix_for_pdf_key
    base_folder, filename = source_key.rsplit("/", 1)
    base_name = filename.rsplit(".", 1)[0]
    split_prefix = f"{base_folder}/split/{base_name}_doc_"

    reader = PdfReader(io.BytesIO(pdf_bytes))
    split_keys: list[str] = []

    for page_index in range(page_count):
        split_key = _upload_split_page_object(
            r2_client,
            bucket,
            reader,
            page_index,
            split_prefix,
        )
        split_keys.append(split_key)

    return split_keys


def _upload_split_page_object(
    r2_client: Any,
    bucket: str,
    reader: Any,
    page_index: int,
    split_prefix: str,
) -> str:
    """Extract one scanned page and upload it to R2.

    Returns a PNG key for the extracted raster page image.

    Raises:
        RuntimeError: If the PDF page does not expose an extractable image.
    """
    image_payload = _extract_scanned_page_image(reader, page_index)
    page_number = page_index + 1
    if image_payload is None:
        raise RuntimeError(
            "Scanned PDF page could not be extracted as an image. "
            f"Source page {page_number} must contain an embedded raster image."
        )

    split_key = f"{split_prefix}{page_number:04d}.png"
    image_bytes, content_type = image_payload
    r2_client.put_object(
        Bucket=bucket,
        Key=split_key,
        Body=image_bytes,
        ContentType=content_type,
    )
    logger.debug("Uploaded split image page %s (%d bytes)", split_key, len(image_bytes))
    return split_key


def _extract_scanned_page_image(
    reader: Any,
    page_index: int,
) -> tuple[bytes, str] | None:
    """Return a raster image extracted from a scanned PDF page when available."""
    try:
        page = reader.pages[page_index]
        page_images = list(page.images)
    except Exception:
        logger.exception("Failed to inspect images for PDF page %d", page_index + 1)
        return None

    if not page_images:
        return None

    best_image = max(page_images, key=lambda image_file: len(image_file.data))
    image = best_image.image
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), "image/png"


def download_r2_bytes(r2_key: str) -> bytes:
    """Download raw image bytes from R2, resolving virtual PDF page keys.

    Transient errors (R2 5xx, throttling, connection / timeout) raise
    :class:`scans.document_ai.TransientOCRError` so the per-scan Celery task
    can retry. Non-existent keys raise :class:`FileNotFoundError` (terminal).
    Anything else surfaces as :class:`RuntimeError` (terminal).
    """
    from django.conf import settings

    from core.storage_backends import get_r2_client, r2_enabled

    if not r2_enabled():
        raise RuntimeError("R2 storage is not configured.")

    source_key, page_number = parse_virtual_pdf_page_key(r2_key)
    client = get_r2_client()

    try:
        response = client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=source_key)
        source_bytes: bytes = response["Body"].read()  # type: ignore[assignment]
    except client.exceptions.NoSuchKey:
        raise FileNotFoundError(f"R2 object not found: {source_key}") from None
    except Exception as exc:
        if _is_transient_r2_error(exc):
            from scans.document_ai import TransientOCRError

            raise TransientOCRError(
                f"Transient R2 failure downloading {source_key}: {exc}"
            ) from exc
        raise RuntimeError(f"Failed to download from R2: {exc}") from exc

    if page_number is None:
        return source_bytes
    return _extract_pdf_page_bytes(source_bytes, source_key, r2_key, page_number)


def _is_transient_r2_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a transient R2/S3 error worth retrying.

    Treats botocore ``ClientError`` with 5xx status, ``EndpointConnectionError``,
    and stdlib network errors as transient. Everything else (auth failures,
    bad bucket names, malformed requests) is permanent.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    try:
        from botocore.exceptions import (  # type: ignore[import-untyped]
            ClientError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
    except ImportError:  # pragma: no cover — botocore is required at runtime
        return False
    if isinstance(exc, (EndpointConnectionError, ReadTimeoutError)):
        return True
    if isinstance(exc, ClientError):
        status = (
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if isinstance(getattr(exc, "response", None), dict)
            else None
        )
        return isinstance(status, int) and status >= 500
    return False


def _image_bytes_to_pdf_bytes(image_bytes: bytes, source_label: str) -> bytes:
    """Convert raster image bytes into a single-page PDF byte stream."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            pdf_image = image.convert("RGB") if image.mode != "RGB" else image.copy()
        output = io.BytesIO()
        pdf_image.save(output, format="PDF")
        return output.getvalue()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to convert image object to PDF for {source_label}: {exc}"
        ) from exc


def _append_object_bytes_to_pdf_writer(
    writer: Any,
    object_bytes: bytes,
    source_label: str,
) -> None:
    """Append a PDF page or raster image object to a PdfWriter."""
    from pypdf import PdfReader

    content_bytes = object_bytes
    if object_bytes[:4] != b"%PDF":
        content_bytes = _image_bytes_to_pdf_bytes(object_bytes, source_label)

    reader = PdfReader(io.BytesIO(content_bytes))
    for page in reader.pages:
        writer.add_page(page)


def build_pdf_bytes_from_r2_keys(page_keys: list[str]) -> bytes:
    """Build a merged PDF byte stream from R2 page keys.

    This is kept for donor review/export. Split intake storage is image-only,
    but the QA viewer still needs a single PDF representation of grouped pages.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for key in page_keys:
        _append_object_bytes_to_pdf_writer(writer, download_r2_bytes(key), key)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _extract_pdf_page_bytes(
    source_bytes: bytes,
    source_key: str,
    r2_key: str,
    page_number: int,
) -> bytes:
    """Extract a single page from a PDF source object."""
    if source_bytes[:4] != b"%PDF":
        raise RuntimeError(
            f"Virtual page key requires PDF source, got non-PDF object: {source_key}"
        )

    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(source_bytes))
        if page_number > len(reader.pages):
            raise RuntimeError(
                "Virtual page index out of range for source PDF "
                f"{source_key}: page {page_number} of {len(reader.pages)}"
            )

        writer = PdfWriter()
        writer.add_page(reader.pages[page_number - 1])
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to extract PDF page from virtual key {r2_key}: {exc}"
        ) from exc


def extract_urn_from_key(r2_key: str) -> str:
    """Extract a potential URN from an R2 object key filename."""
    source_key, page_number = parse_virtual_pdf_page_key(r2_key)
    filename = source_key.rsplit("/", 1)[-1]
    base_urn = filename.rsplit(".", 1)[0] if "." in filename else filename
    if page_number is None:
        return base_urn
    return f"{base_urn}_p{page_number:04d}"


def expand_pdf_keys_for_campaign(r2_keys: list[str]) -> list[str]:
    """Expand multi-page PDF keys by pre-splitting pages into R2.

    For every ``.pdf`` key with more than one page, each page is uploaded as
    an individual single-page PDF under the ``split/`` sub-prefix.  The
    returned keys are real R2 object keys — no virtual ``::pdf_page::``
    pointers are created.  OCR workers therefore download a small per-page
    file rather than the full batch PDF on every donor pass.

    Keys that are already virtual (legacy ``::pdf_page::`` format) or that
    are not PDFs are passed through unchanged.
    """
    expanded_keys: list[str] = []
    for r2_key in sorted(r2_keys):
        source_key, page_number = parse_virtual_pdf_page_key(r2_key)
        if page_number is not None:
            # Legacy virtual key — keep as-is for backward compatibility
            expanded_keys.append(r2_key)
            continue
        if not source_key.lower().endswith(".pdf"):
            expanded_keys.append(source_key)
            continue

        pdf_bytes = download_r2_bytes(source_key)
        page_count = count_pdf_pages(pdf_bytes)
        if page_count is None or page_count <= 1:
            # Single-page or unreadable PDF — use the source key directly
            expanded_keys.append(source_key)
            continue

        split_keys = _split_pdf_pages_to_r2(source_key, pdf_bytes, page_count)
        expanded_keys.extend(split_keys)
        logger.info(
            "Pre-split PDF %s into %d page files under split/ in R2",
            source_key,
            page_count,
        )
    return expanded_keys


def build_placeholder(
    scan_batch: Any,
    r2_key: str,
    page_keys: list[str] | None = None,
) -> Any:
    """Build an unsaved ScanPlaceholder for a grouped donor document."""
    from core.storage_backends import r2_public_url
    from scans.models import ScanPlaceholder

    source_key, page_number = parse_virtual_pdf_page_key(r2_key)
    image_url = r2_public_url(source_key)
    if page_number is not None:
        image_url = f"{image_url}#page={page_number}"

    pk_list = list(page_keys) if page_keys else []
    snapshot = pk_list if pk_list else [r2_key]

    # Always start as PENDING. The RedactionSettings singleton drives whether
    # this placeholder's eventual payment method actually requires redaction
    # before approval — gates consult it dynamically, so a PENDING status on a
    # cheque/cash placeholder is a no-op.
    return ScanPlaceholder(
        batch=scan_batch,
        image_url=image_url,
        image_path=r2_key,
        urn=extract_urn_from_key(r2_key),
        page_keys=pk_list,
        original_page_keys=list(snapshot),
        redaction_status=ScanPlaceholder.REDACTION_PENDING,
        redaction_completed_at=None,
        ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
        extracted_data={},
    )


def generate_batch_name() -> str:
    """Generate an auto-incrementing scan batch name."""
    suffix = "".join(random.choices(string.digits, k=5))
    return f"SCN-{timezone.now().strftime('%d%H')}-{suffix}"


def _resolve_campaign(campaign_id: str) -> Any:
    """Load a campaign with its client or raise a user-friendly error.

    Also rejects inactive campaigns / deactivated clients so the
    Celery task does not silently accept ingest work that should
    have been blocked at submission time.
    """
    from campaigns.ingest_guards import campaign_scan_block_reason
    from campaigns.models import Campaign

    try:
        campaign = Campaign.objects.select_related("client").get(id=campaign_id)
    except Campaign.DoesNotExist:
        raise ValueError(f"Campaign not found: {campaign_id}") from None

    block_reason = campaign_scan_block_reason(campaign)
    if block_reason is not None:
        raise ValueError(block_reason)
    return campaign


def _validate_scan_form_type(payment_method: str, scan_form_type: str) -> None:
    """Validate the selected layout for the batch payment method."""
    from scans.models import ScanBatch

    ScanBatch.validate_batch_request(payment_method, scan_form_type)


def _group_without_regrouping(
    campaign_id: str,
    r2_keys: list[str],
    scan_form_type: str,
) -> tuple[list[list[str]], int]:
    """Use each uploaded document as a donor document without regrouping."""
    sorted_keys = sorted(r2_keys)
    key_groups = [[key] for key in sorted_keys]
    donor_count = len(key_groups)
    logger.info(
        "Campaign '%s' scan_form_type=%s → using %d input documents without "
        "software regrouping",
        campaign_id,
        scan_form_type,
        donor_count,
    )
    return key_groups, donor_count


def _group_expanded_keys(
    campaign_id: str,
    r2_keys: list[str],
    scan_form_type: str,
    pages_per_donor: int,
) -> tuple[list[list[str]], int]:
    """Expand PDFs and group the resulting keys by pages-per-donor."""
    expanded_keys = expand_pdf_keys_for_campaign(r2_keys)
    sorted_keys = sorted(expanded_keys)
    key_groups = [
        sorted_keys[index : index + pages_per_donor]
        for index in range(0, len(sorted_keys), pages_per_donor)
    ]
    donor_count = len(key_groups)
    if pages_per_donor > 1:
        logger.info(
            "Campaign '%s' scan_form_type=%s → grouping %d pages into %d donor "
            "documents (%d pages each)",
            campaign_id,
            scan_form_type,
            len(sorted_keys),
            donor_count,
            pages_per_donor,
        )
    else:
        logger.info(
            "Campaign '%s' scan_form_type=%s → using 1 page per donor (%d documents)",
            campaign_id,
            scan_form_type,
            donor_count,
        )
    return key_groups, donor_count


def group_keys_for_layout(
    campaign_id: str,
    r2_keys: list[str],
    scan_form_type: str,
) -> tuple[list[list[str]], int]:
    """Build grouped page keys for the selected scan form layout."""
    from scans.models import ScanBatch

    pages_per_donor = ScanBatch.scan_form_type_pages_per_donor(scan_form_type)
    if pages_per_donor is None:
        return _group_without_regrouping(campaign_id, r2_keys, scan_form_type)
    return _group_expanded_keys(
        campaign_id,
        r2_keys,
        scan_form_type,
        pages_per_donor,
    )


def create_scan_batch_from_r2(
    campaign_id: str,
    r2_keys: list[str],
    payment_method: str,
    scan_form_type: str,
    user: Any | None = None,
    batch_name: str = "",
) -> Any:
    """Create a ScanBatch and ScanPlaceholders from uploaded R2 keys."""
    from scans.models import ScanBatch, ScanPlaceholder

    if not r2_keys:
        raise ValueError("No R2 keys provided for scan batch.")

    campaign = _resolve_campaign(campaign_id)
    resolved_batch_name = batch_name or generate_batch_name()
    _validate_scan_form_type(payment_method, scan_form_type)
    key_groups, donor_count = group_keys_for_layout(
        campaign_id,
        r2_keys,
        scan_form_type,
    )

    with transaction.atomic():
        scan_batch = ScanBatch.objects.create(
            campaign=campaign,
            batch_name=resolved_batch_name,
            payment_method=payment_method,
            scan_form_type=scan_form_type,
            total_scans=donor_count,
            created_by=user,
        )
        placeholders = [
            build_placeholder(
                scan_batch,
                group[0],
                page_keys=group if len(group) > 1 else [],
            )
            for group in key_groups
        ]
        ScanPlaceholder.objects.bulk_create(placeholders)

    logger.info(
        "Created scan batch '%s' with %d donor documents for campaign %s "
        "(scan_form_type=%s, scan_purpose=%s)",
        resolved_batch_name,
        donor_count,
        campaign_id,
        scan_form_type,
        campaign.scan_purpose,
    )
    return scan_batch
