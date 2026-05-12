"""Google Document AI service for scanned donation form processing.

Supports two processor types:

* **Custom Document Extractor** — trained processor; entities returned via
  ``document.entities``.
* **Form Parser** — pre-trained processor; key-value pairs returned via
  ``document.pages[i].form_fields``.

Both are normalised into the same :class:`DocumentAIEntity` list so that
:mod:`scans.ocr` can apply ``_ENTITY_FIELD_MAP`` overrides
without needing to know which processor type was used.

Each charity has its own processor whose resource ID is stored on the
:class:`~core.models.Client` model.  This service is responsible only for
calling the API and returning structured results; field extraction and donor
matching are handled by :mod:`scans.ocr`.

Reliability features:

* Transient Google API errors (``ResourceExhausted``/429,
  ``ServiceUnavailable``/503, network ``RetryError``) are retried with
  exponential-jitter backoff via :mod:`tenacity`. Permanent errors
  (``InvalidArgument``, ``PermissionDenied``, ``NotFound``) surface
  immediately so retries never burn quota on broken inputs.
* The ``DocumentProcessorServiceClient`` is cached at module level keyed on
  ``(project_id, location)`` and the SHA-256 hash of the credential JSON
  env var. A TTL forces rebuild on long-lived workers; rotating the
  credential JSON invalidates the cache eagerly.

Usage::

    from scans.document_ai import DocumentAIService

    result = DocumentAIService.process_image_bytes(image_bytes, client)
    print(result.full_text)
    for entity in result.entities:
        print(entity.type_, entity.mention_text, entity.confidence)
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


class PDFTooLargeError(RuntimeError):
    """Raised when a PDF exceeds ``MAX_OCR_PAGES_PER_PDF``.

    Carries the observed page count and the limit so the webhook layer can
    surface a meaningful 413 response. Subclasses :class:`RuntimeError` so
    existing ``except RuntimeError`` blocks in the OCR pipeline keep working.
    """

    def __init__(self, page_count: int, limit: int) -> None:
        self.page_count = page_count
        self.limit = limit
        super().__init__(
            f"PDF has {page_count} pages, exceeds MAX_OCR_PAGES_PER_PDF={limit}"
        )


class TransientOCRError(Exception):
    """Raised when OCR/storage failed transiently and the call should be retried.

    Distinguishes "this could succeed if we try again later" (Document AI 503,
    R2 5xx, network glitch) from terminal errors (bad PDF, missing key,
    permission denied). ``process_single_scan`` re-raises this so the per-scan
    Celery task's ``max_retries`` actually fires; everything else is treated
    as terminal and marks the placeholder ``OCR_STATUS_FAILED`` immediately.
    """


@dataclass
class DocumentAIEntity:
    """A single extracted entity from a Document AI processor.

    Attributes:
        type_: Entity type label — either the trained entity type from a Custom
            Extractor (e.g. ``"donation_amount"``) or the verbatim form field
            label from a Form Parser page (e.g. ``"Telephone"``).
        mention_text: Raw text of the extracted value as it appears in the form.
        confidence: Extraction confidence score from 0.0 to 1.0.
        normalised_value: Optional normalised/parsed value from Doc AI
            (e.g., Money proto for amounts, Date proto for dates).
        source: Origin of the entity: ``"nlp"`` for ``document.entities``
            (document-wide NLP detection) or ``"form_field"`` for individual
            labeled key-value pairs from ``page.form_fields``.  This is used
            to prevent document-wide NLP phone/email detections (which find the
            charity's helpline) from overwriting per-field label matches.
    """

    type_: str
    mention_text: str
    confidence: float
    normalised_value: str = ""
    source: str = "nlp"  # "nlp" | "form_field"


@dataclass
class DocumentAIResult:
    """Parsed result from a Document AI process_document call.

    Attributes:
        full_text: Complete UTF-8 text from the document (OCR layer).
        entities: Extracted entities from the Custom Extractor processor.
        confidence: Average entity confidence across all extracted entities.
        page_count: Number of pages in the processed document.
        raw_response: JSON-serialisable summary for storage in
            ``ScanPlaceholder.ocr_data``.
    """

    full_text: str
    entities: list[DocumentAIEntity]
    confidence: float
    page_count: int = 1
    raw_response: dict[str, Any] = field(default_factory=dict)


class DocumentAIService:
    """Google Document AI integration for donation form OCR and entity extraction.

    Each charity uses a dedicated custom-trained extractor processor whose ID
    is stored in ``Client.document_ai_processor_id``. Authentication supports
    either:
    - ``GOOGLE_APPLICATION_CREDENTIALS`` (path to service account JSON file),
    - ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` (raw JSON in env),
    - ``GOOGLE_APPLICATION_CREDENTIALS_JSON_B64`` (base64-encoded JSON).

    All methods are static — no instance state required.
    """

    @staticmethod
    def is_configured(client: Any) -> bool:
        """Check whether Document AI is properly configured for a client.

        Args:
            client: Client model instance.

        Returns:
            True if the project ID is set in settings and the client has a
            processor ID configured.
        """
        project_id = getattr(settings, "GOOGLE_CLOUD_PROJECT_ID", "")
        processor_id = getattr(client, "document_ai_processor_id", "")
        return bool(project_id and processor_id)

    @staticmethod
    def _get_processor_name(client: Any) -> str:
        """Build the full Document AI processor resource name.

        The ``document_ai_processor_id`` field on the client may hold any of:
        - A bare processor ID (e.g. ``"abc123def456"``).
        - A full resource name (``"projects/…/locations/…/processors/…"``).

        If a full resource name is already stored we use it directly; otherwise
        we construct the canonical path from project ID + location + ID.

        Args:
            client: Client model instance.

        Returns:
            Full processor resource name string.
        """
        processor_id: str = client.document_ai_processor_id.strip()

        # Already a full resource name
        if processor_id.startswith("projects/"):
            return processor_id

        project_id: str = getattr(settings, "GOOGLE_CLOUD_PROJECT_ID", "")
        location: str = getattr(client, "document_ai_location", "eu") or "eu"
        return f"projects/{project_id}/locations/{location}/processors/{processor_id}"

    @staticmethod
    def _build_client(location: str) -> Any:
        """Construct a fresh ``DocumentProcessorServiceClient`` from env credentials.

        Supports credentials from file path, raw JSON env var, or base64 JSON
        env var. Raises :class:`RuntimeError` if credentials are absent or
        invalid. Callers should normally use :meth:`_get_client`, which caches
        the result; this method is the cache miss path.

        Args:
            location: GCP region for the processor endpoint (e.g. ``"eu"``).

        Returns:
            DocumentProcessorServiceClient.

        Raises:
            RuntimeError: If credentials are not configured.
        """
        from google.cloud import documentai  # type: ignore[import-untyped]
        from google.oauth2 import service_account  # type: ignore[import-untyped]

        credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
        credentials_json_b64 = os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", ""
        ).strip()

        configured_sources: list[str] = []
        if credentials_json_b64:
            configured_sources.append("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64")
        if credentials_json:
            configured_sources.append("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if credentials_file:
            configured_sources.append("GOOGLE_APPLICATION_CREDENTIALS")

        if len(configured_sources) > 1:
            logger.warning(
                "Multiple Document AI credential sources are configured (%s). "
                "Using precedence: GOOGLE_APPLICATION_CREDENTIALS_JSON_B64 > "
                "GOOGLE_APPLICATION_CREDENTIALS_JSON > "
                "GOOGLE_APPLICATION_CREDENTIALS.",
                ", ".join(configured_sources),
            )

        credentials: Any | None = None

        if credentials_json_b64:
            try:
                decoded_json = base64.b64decode(credentials_json_b64).decode("utf-8")
                credentials_info = json.loads(decoded_json)
                private_key = credentials_info.get("private_key")
                if isinstance(private_key, str):
                    credentials_info["private_key"] = private_key.replace("\\n", "\n")
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info
                )
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64 is invalid."
                ) from exc
        elif credentials_json:
            try:
                credentials_info = json.loads(credentials_json)
                private_key = credentials_info.get("private_key")
                if isinstance(private_key, str):
                    credentials_info["private_key"] = private_key.replace("\\n", "\n")
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info
                )
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    "GOOGLE_APPLICATION_CREDENTIALS_JSON is invalid JSON."
                ) from exc
        elif credentials_file:
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    credentials_file
                )
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    "GOOGLE_APPLICATION_CREDENTIALS points to an invalid file."
                ) from exc
        else:
            raise RuntimeError(
                "Document AI credentials are not configured. Set one of "
                "GOOGLE_APPLICATION_CREDENTIALS, "
                "GOOGLE_APPLICATION_CREDENTIALS_JSON, or "
                "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64."
            )

        # Use the regional endpoint so data stays within the chosen region.
        api_endpoint = f"{location}-documentai.googleapis.com"
        return documentai.DocumentProcessorServiceClient(
            client_options={"api_endpoint": api_endpoint},
            credentials=credentials,
        )

    @staticmethod
    def _get_client(location: str = "eu") -> Any:
        """Return a cached ``DocumentProcessorServiceClient``.

        Constructing the client performs an OAuth handshake; in a 5-scanner
        burst this used to fire ~3 handshakes per OCR. We cache the client at
        module level keyed on ``(project_id, location, credentials_hash)``
        and rebuild it when:

        * the cache TTL (``DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS``) elapses, or
        * the credential JSON env var rotates (hash mismatch), or
        * the cached entry is for a different ``(project_id, location)``.

        Thread-safe: a module-level lock guards cache writes so concurrent
        Celery workers don't race on first-use.

        Args:
            location: GCP region for the processor endpoint (e.g. ``"eu"``).

        Returns:
            Cached or freshly built DocumentProcessorServiceClient.
        """
        project_id = str(getattr(settings, "GOOGLE_CLOUD_PROJECT_ID", ""))
        creds_hash = _credentials_hash()
        cache_key = (project_id, location, creds_hash)
        ttl = int(
            getattr(settings, "DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS", 3600) or 3600
        )

        with _CLIENT_CACHE_LOCK:
            cached = _CLIENT_CACHE.get(cache_key)
            if cached is not None:
                client_obj, built_at = cached
                if (time.monotonic() - built_at) < ttl:
                    return client_obj
                logger.info(
                    "Document AI client cache expired after %ds; rebuilding", ttl
                )
            elif _CLIENT_CACHE:
                # A different key is cached — credentials rotated or
                # location changed. Drop everything so we don't leak the
                # previous client past credential rotation.
                logger.info(
                    "Document AI credentials/location changed; invalidating client cache"
                )
                _CLIENT_CACHE.clear()

            new_client = DocumentAIService._build_client(location)
            _CLIENT_CACHE[cache_key] = (new_client, time.monotonic())
            return new_client

    @staticmethod
    def process_image_bytes(image_bytes: bytes, client: Any) -> DocumentAIResult:
        """Process a scanned donation form image using the client's Custom Extractor.

        Calls the Document AI ``process_document`` endpoint and returns a
        structured :class:`DocumentAIResult` containing the full OCR text and
        all extracted entities as classified by the trained processor.

        Transient Google API errors (429/503/network) are retried with
        exponential-jitter backoff (see :func:`_call_process_document`).
        Permanent errors (``InvalidArgument``, ``PermissionDenied``,
        ``NotFound``) are surfaced immediately as :class:`RuntimeError`.

        Args:
            image_bytes: Raw image bytes (JPEG, PNG, TIFF, or single-page PDF).
            client: Client model instance with ``document_ai_processor_id`` set.

        Returns:
            DocumentAIResult with OCR text and extracted entities.

        Raises:
            RuntimeError: If Document AI is not configured, the input is
                rejected as permanently invalid, or transient retries are
                exhausted.
            PDFTooLargeError: If the PDF exceeds ``MAX_OCR_PAGES_PER_PDF``.
        """
        if not DocumentAIService.is_configured(client):
            raise RuntimeError(
                f"Document AI is not configured for client '{client.name}'. "
                "Set document_ai_processor_id on the client record."
            )

        from google.cloud import documentai  # type: ignore[import-untyped]

        location: str = getattr(client, "document_ai_location", "eu") or "eu"
        processor_name = DocumentAIService._get_processor_name(client)
        doc_ai_client = DocumentAIService._get_client(location)

        # Detect MIME type from magic bytes
        mime_type = _detect_mime_type(image_bytes)

        # Reject oversized PDFs early so we never burn quota on accidental
        # multi-hundred-page scanner dumps.
        if mime_type == "application/pdf":
            _enforce_pdf_page_limit(image_bytes)

        raw_document = documentai.RawDocument(
            content=image_bytes,
            mime_type=mime_type,
        )

        request = documentai.ProcessRequest(
            name=processor_name,
            raw_document=raw_document,
            # Enable imageless mode to allow processing PDFs up to 30 pages.
            # Without this, the limit is 15 pages and larger PDFs fail with
            # INVALID_ARGUMENT.
            imageless_mode=True,
        )

        try:
            response = _call_process_document(doc_ai_client, request)
        except PDFTooLargeError:
            raise
        except RetryError as exc:
            logger.exception(
                "Document AI transient retries exhausted for client '%s' processor '%s'",
                client.name,
                processor_name,
            )
            inner = exc.last_attempt.exception() if exc.last_attempt else exc
            # Surface as TransientOCRError so the per-scan Celery task can
            # retry with a fresh worker / longer backoff. Tenacity's intra-call
            # retries cover seconds-scale flapping; Celery covers
            # minutes-scale region/queue outages.
            raise TransientOCRError(
                f"Document AI API call failed after retries: {inner}"
            ) from exc
        except Exception as exc:
            logger.exception(
                "Document AI API call failed for client '%s' processor '%s'",
                client.name,
                processor_name,
            )
            raise RuntimeError(f"Document AI API call failed: {exc}") from exc

        return _parse_document_ai_response(response.document)

    @staticmethod
    def process_image_from_r2(r2_key: str, client: Any) -> DocumentAIResult:
        """Download an image from R2 and process it through Document AI.

        Args:
            r2_key: The R2 object key (e.g. ``"ScanOutput/APPEAL001/cheque/URN.jpg"``).
            client: Client model instance.

        Returns:
            DocumentAIResult (same format as process_image_bytes).

        Raises:
            FileNotFoundError: If the R2 object does not exist.
            RuntimeError: If Document AI processing fails.
        """
        from core.storage_backends import get_r2_client, r2_enabled

        if not r2_enabled():
            raise RuntimeError("R2 storage is not configured.")

        r2 = get_r2_client()
        bucket = settings.R2_BUCKET_NAME

        try:
            response = r2.get_object(Bucket=bucket, Key=r2_key)
            image_bytes: bytes = response["Body"].read()
        except r2.exceptions.NoSuchKey:
            raise FileNotFoundError(f"R2 object not found: {r2_key}") from None
        except Exception as exc:
            raise RuntimeError(f"Failed to download from R2: {exc}") from exc

        logger.info(
            "Document AI: downloaded %s from R2 (%d bytes)", r2_key, len(image_bytes)
        )
        return DocumentAIService.process_image_bytes(image_bytes, client)


# ─── Private helpers ───────────────────────────────────────────────────────────


# Module-level client cache. Key: (project_id, location, credentials_hash).
# Value: (client, monotonic-time-built). Guarded by ``_CLIENT_CACHE_LOCK``.
_CLIENT_CACHE: dict[tuple[str, str, str], tuple[Any, float]] = {}
_CLIENT_CACHE_LOCK = threading.Lock()


def _credentials_hash() -> str:
    """Return a SHA-256 hash fingerprint of the active credential env var.

    Used as a cache-invalidation signal: rotating
    ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` (or its base64 sibling, or the
    file path) flips the hash and forces a fresh client build.
    """
    sources = (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", ""),
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", ""),
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
    )
    return hashlib.sha256(("|".join(sources)).encode("utf-8")).hexdigest()


def _reset_client_cache() -> None:
    """Drop all cached Document AI clients. Test-only helper."""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()


class _TransientGoogleAPIError(Exception):
    """Internal sentinel wrapping retryable Google API errors.

    Defined as a plain :class:`Exception` subclass so tenacity's
    ``retry_if_exception_type`` filter is unambiguous. The original Google
    exception is preserved as ``__cause__`` for logging.
    """


def _is_transient_documentai_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a transient Google API error worth retrying.

    Transient: ``ResourceExhausted`` (429), ``ServiceUnavailable`` (503),
    ``DeadlineExceeded``, ``RetryError`` (network/socket), and generic
    ``ConnectionError`` / ``TimeoutError``. Everything else (notably
    ``InvalidArgument``, ``PermissionDenied``, ``NotFound``) is permanent
    and surfaces immediately.
    """
    try:
        from google.api_core import (
            exceptions as gax_exc,  # type: ignore[import-untyped]
        )
    except ImportError:  # pragma: no cover — google deps are required at runtime
        return isinstance(exc, (ConnectionError, TimeoutError))

    transient: tuple[type[BaseException], ...] = (
        gax_exc.ResourceExhausted,
        gax_exc.ServiceUnavailable,
        gax_exc.DeadlineExceeded,
        gax_exc.RetryError,
        ConnectionError,
        TimeoutError,
    )
    return isinstance(exc, transient)


def _call_process_document(doc_ai_client: Any, request: Any) -> Any:
    """Invoke ``process_document`` with tenacity-managed retries.

    Wraps the raw ``DocumentProcessorServiceClient.process_document`` call so
    that transient 429/503/network errors are retried with exponential-jitter
    backoff (capped at 60s) up to ``DOCUMENT_AI_MAX_RETRY_ATTEMPTS`` times.
    Permanent errors (``InvalidArgument`` / ``PermissionDenied`` /
    ``NotFound``) escape on the first occurrence — retrying a bad PDF or a
    misconfigured processor would just burn quota.

    Args:
        doc_ai_client: Cached ``DocumentProcessorServiceClient``.
        request: ``documentai.ProcessRequest`` proto.

    Returns:
        The raw ``ProcessResponse``.

    Raises:
        tenacity.RetryError: When transient retries are exhausted.
        Exception: Any permanent (non-transient) Google API error escapes
            unchanged.
    """
    max_attempts = int(getattr(settings, "DOCUMENT_AI_MAX_RETRY_ATTEMPTS", 5) or 5)

    @retry(
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(initial=1, max=60),
        retry=retry_if_exception_type(_TransientGoogleAPIError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,
    )
    def _attempt() -> Any:
        try:
            return doc_ai_client.process_document(request=request)
        except Exception as exc:
            if _is_transient_documentai_error(exc):
                # Re-raise tagged so tenacity's filter matches reliably even
                # if the underlying class hierarchy varies between API
                # versions or test mocks.
                raise _TransientGoogleAPIError(str(exc)) from exc
            raise

    return _attempt()


def _enforce_pdf_page_limit(pdf_bytes: bytes) -> None:
    """Raise :class:`PDFTooLargeError` if a PDF exceeds the page-count limit.

    Returns silently if the page count is at or below
    ``MAX_OCR_PAGES_PER_PDF`` or if the PDF is unreadable (in which case
    Document AI will raise ``InvalidArgument`` and surface a permanent
    failure to the operator).
    """
    from scans.scan_processing_r2 import count_pdf_pages

    limit = int(getattr(settings, "MAX_OCR_PAGES_PER_PDF", 100) or 100)
    page_count = count_pdf_pages(pdf_bytes)
    if page_count is None:
        return
    if page_count > limit:
        raise PDFTooLargeError(page_count, limit)


def _detect_mime_type(image_bytes: bytes) -> str:
    """Detect image MIME type from magic bytes.

    Args:
        image_bytes: Raw image data.

    Returns:
        MIME type string suitable for Document AI RawDocument.
    """
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if image_bytes[:4] == b"%PDF":
        return "application/pdf"
    # Default to JPEG for scanner output
    return "image/jpeg"


def _extract_normalised_value(entity: Any) -> str:
    """Extract a human-readable normalised value from a Document AI entity.

    Document AI may provide structured values (Money, Date, Address) alongside
    raw mention text.  This helper returns a string representation.

    Args:
        entity: A ``documentai.Document.Entity`` proto instance.

    Returns:
        Normalised string value, or empty string if not available.
    """
    try:
        nv = entity.normalised_value
        # Money proto
        if nv.money_value.units or nv.money_value.nanos:
            units = nv.money_value.units
            nanos = nv.money_value.nanos
            return f"{units}.{abs(nanos) // 10_000_000:02d}"
        # Date proto
        if nv.date_value.year:
            d = nv.date_value
            return f"{d.day:02d}/{d.month:02d}/{d.year}"
        # Boolean
        if nv.boolean_value is not None and str(nv) != "":
            return str(nv.boolean_value)
        # Text
        if nv.text:
            return nv.text
    except Exception:
        pass
    return ""


def _text_from_anchor(text_anchor: Any, full_text: str) -> str:
    """Extract plain text from a Document AI ``TextAnchor``.

    The ``content`` attribute is preferred when present (newer SDK versions).
    Falls back to reconstructing the text from ``text_segments`` start/end
    offsets into the document full-text.

    Args:
        text_anchor: ``documentai.Document.TextAnchor`` proto.
        full_text: Full OCR text from the document (``document.text``).

    Returns:
        Extracted text string, possibly empty.
    """
    content: str = getattr(text_anchor, "content", "") or ""
    if content:
        return content

    segments = getattr(text_anchor, "text_segments", []) or []
    parts: list[str] = []
    for seg in segments:
        start = int(getattr(seg, "start_index", 0) or 0)
        end = int(getattr(seg, "end_index", 0) or 0)
        if end > start and end <= len(full_text):
            parts.append(full_text[start:end])
    return "".join(parts)


def _collect_entities(document: Any) -> tuple[list[DocumentAIEntity], list[float]]:
    """Collect entities from a Document AI Document proto.

    Handles both processor types:

    * **Custom Extractor**: reads ``document.entities`` and nested
      ``entity.properties`` sub-entities.
    * **Form Parser**: reads ``document.pages[i].form_fields`` and converts
      each key-value pair into a :class:`DocumentAIEntity` where
      ``type_`` is the field label and ``mention_text`` is the field value.

    Args:
        document: ``google.cloud.documentai.Document`` proto.

    Returns:
        Tuple of (entity list, confidence list).
    """
    entities: list[DocumentAIEntity] = []
    confidences: list[float] = []
    full_text: str = getattr(document, "text", "") or ""

    # ── Custom Extractor: top-level entities + nested sub-entities ─────────────
    for entity in document.entities:
        entity_type = str(entity.type_).strip()
        mention_text = str(entity.mention_text).strip()
        confidence = float(entity.confidence)
        normalised = _extract_normalised_value(entity)

        entities.append(
            DocumentAIEntity(
                type_=entity_type,
                mention_text=mention_text,
                confidence=confidence,
                normalised_value=normalised,
            )
        )
        confidences.append(confidence)

        # Also parse nested sub-entities (e.g., address components)
        for prop in entity.properties:
            sub_type = str(prop.type_).strip()
            sub_text = str(prop.mention_text).strip()
            sub_conf = float(prop.confidence)
            sub_norm = _extract_normalised_value(prop)
            entities.append(
                DocumentAIEntity(
                    type_=sub_type,
                    mention_text=sub_text,
                    confidence=sub_conf,
                    normalised_value=sub_norm,
                )
            )
            confidences.append(sub_conf)

    # ── Form Parser: page-level key-value form fields ───────────────────────
    # Form Parser does not populate document.entities; instead each page
    # has a list of FormField objects (field name + field value).
    for page in document.pages:
        for ff in page.form_fields:
            key_text = (
                _text_from_anchor(ff.field_name.text_anchor, full_text)
                .strip()
                .rstrip(":")
            )
            confidence = float(getattr(ff.field_value, "confidence", 0.0) or 0.0)

            # Checkboxes: value_type takes precedence over text_anchor content
            value_type = str(getattr(ff.field_value, "value_type", "") or "")
            if value_type in ("filled_checkbox", "unfilled_checkbox"):
                val_text = "yes" if value_type == "filled_checkbox" else "no"
                # Use a higher confidence floor for checkbox state — it is explicit
                confidence = max(confidence, 0.8)
            else:
                val_text = _text_from_anchor(
                    ff.field_value.text_anchor, full_text
                ).strip()

            if not key_text or not val_text:
                continue

            entities.append(
                DocumentAIEntity(
                    type_=key_text,
                    mention_text=val_text,
                    confidence=max(confidence, 0.5),  # Form Parser min floor
                    normalised_value="",
                    source="form_field",
                )
            )
            confidences.append(confidence)

    return entities, confidences


def _parse_document_ai_response(document: Any) -> DocumentAIResult:
    """Parse a Document AI Document proto into a :class:`DocumentAIResult`.

    Args:
        document: ``google.cloud.documentai.Document`` proto.

    Returns:
        Structured DocumentAIResult.
    """
    full_text: str = getattr(document, "text", "") or ""
    entities, confidences = _collect_entities(document)

    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    page_count = len(document.pages)

    raw_response: dict[str, Any] = {
        "full_text_length": len(full_text),
        "entity_count": len(entities),
        "page_count": page_count,
        "entity_types": [e.type_ for e in entities],
    }

    return DocumentAIResult(
        full_text=full_text,
        entities=entities,
        confidence=round(avg_confidence, 4),
        page_count=page_count,
        raw_response=raw_response,
    )
