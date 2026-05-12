"""OCR and extraction helpers for scan processing."""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from scans.scan_processing_r2 import download_r2_bytes

logger = logging.getLogger(__name__)


def decode_qr_from_scan(placeholder: Any, image_bytes: bytes) -> dict[str, str] | None:
    """Attempt QR decode and update the placeholder with the result.

    Stores the discriminated QR result kind (absent / decoded / malformed)
    in ``placeholder.ocr_data['qr_kind']`` so downstream donor-matching can
    distinguish a missing sticker (cold record) from a corrupt one (rescan).

    Returns the parsed payload dict for DECODED records (back-compat with
    OCRExtractor.extract_from_document_ai) or ``None`` otherwise.
    """
    from scans.qr import QRResultKind, QRService

    result = QRService.decode(image_bytes)
    placeholder.qr_decoded = result.kind is QRResultKind.DECODED

    ocr_data = dict(placeholder.ocr_data or {})
    ocr_data["qr_kind"] = result.kind.value
    if result.kind is QRResultKind.MALFORMED:
        ocr_data["qr_error"] = result.error
    placeholder.ocr_data = ocr_data

    if result.kind is QRResultKind.ABSENT:
        logger.debug("No QR code found for scan %s — cold record path", placeholder.id)
        return None

    if result.kind is QRResultKind.MALFORMED:
        logger.warning(
            "Malformed QR for scan %s — flagging for rescan: %s",
            placeholder.id,
            result.error,
        )
        return None

    placeholder.qr_raw = QRService.encode_payload(
        result.appeal_code,
        result.package_code,
        result.urn,
    )
    logger.info(
        "QR decoded for scan %s — warm record: appeal=%s pkg=%s urn=%s",
        placeholder.id,
        result.appeal_code,
        result.package_code,
        result.urn,
    )
    return result.to_payload_dict()


def run_document_ai(placeholder: Any, image_bytes: bytes, client: Any) -> Any:
    """Run Document AI on image bytes and store results on the placeholder."""
    from core.services.field_normalizer import FieldNormalizer
    from scans.document_ai import DocumentAIService

    doc_ai_result = DocumentAIService.process_image_bytes(image_bytes, client)
    unmapped_entity_labels = FieldNormalizer.find_unmapped_labels(
        doc_ai_result.entities,
        client,
    )
    existing_ocr_data = placeholder.ocr_data or {}
    new_ocr_data: dict[str, Any] = {
        "full_text": doc_ai_result.full_text[:10_000],
        "confidence": doc_ai_result.confidence,
        "entity_count": len(doc_ai_result.entities),
        "page_count": doc_ai_result.page_count,
        "processor": client.document_ai_processor_id,
        "qr_decoded": placeholder.qr_decoded,
        "qr_kind": existing_ocr_data.get("qr_kind", ""),
        "unmapped_entity_labels": unmapped_entity_labels,
        **doc_ai_result.raw_response,
    }
    qr_error = existing_ocr_data.get("qr_error", "")
    if qr_error:
        new_ocr_data["qr_error"] = qr_error
    placeholder.ocr_data = new_ocr_data
    placeholder.ocr_confidence = doc_ai_result.confidence
    return doc_ai_result


def merge_extracted_multi_page(page_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge OCR extraction results from multiple document pages."""
    if not page_results:
        return {}
    if len(page_results) == 1:
        return page_results[0]

    merged: dict[str, Any] = {}
    confidence_accum: dict[str, list[float]] = {}
    for page_dict in page_results:
        _merge_page_values(merged, confidence_accum, page_dict)

    for base_field, scores in confidence_accum.items():
        confidence_key = f"{base_field}_confidence"
        if confidence_key not in merged:
            merged[confidence_key] = round(sum(scores) / len(scores), 3)
    return merged


def _merge_page_values(
    merged: dict[str, Any],
    confidence_accum: dict[str, list[float]],
    page_dict: dict[str, Any],
) -> None:
    """Merge a single page's extracted values into the aggregate dicts."""
    for key, value in page_dict.items():
        if key.endswith("_confidence"):
            _accumulate_confidence(confidence_accum, key, value)
            continue

        existing = merged.get(key)
        is_empty = existing in (None, "", [], {})
        if key == "gift_aid" and value is True:
            merged[key] = True
        elif (key == "amount" and _should_replace_amount(merged, page_dict, value)) or (
            is_empty and value not in (None, "", [], {})
        ):
            merged[key] = value


def _accumulate_confidence(
    confidence_accum: dict[str, list[float]],
    key: str,
    value: Any,
) -> None:
    """Store a valid confidence score for later averaging."""
    if isinstance(value, (int, float)) and value > 0:
        base_field = key.removesuffix("_confidence")
        confidence_accum.setdefault(base_field, []).append(float(value))


def _parse_amount_value(value: Any) -> Decimal | None:
    """Return a Decimal for a merged OCR amount candidate when possible."""
    if value in (None, "", [], {}):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _should_replace_amount(
    merged: dict[str, Any],
    page_dict: dict[str, Any],
    new_value: Any,
) -> bool:
    """Return whether a later page amount should replace the merged value."""
    existing_value = merged.get("amount")
    if existing_value in (None, "", [], {}):
        return True

    existing_confidence = float(merged.get("amount_confidence", 0.0) or 0.0)
    new_confidence = float(page_dict.get("amount_confidence", 0.0) or 0.0)
    existing_amount = _parse_amount_value(existing_value)
    new_amount = _parse_amount_value(new_value)
    if existing_amount is None or new_amount is None:
        return new_confidence > existing_confidence

    if existing_amount < Decimal("10.00") <= new_amount:
        return True
    if new_confidence > existing_confidence:
        return True
    if new_confidence < existing_confidence:
        return False

    return False


def derive_title_from_name(extracted: dict[str, Any]) -> None:
    """Populate a title by splitting the salutation from donor_name."""
    normalise = {
        "mr": "Mr",
        "mrs": "Mrs",
        "ms": "Ms",
        "miss": "Miss",
        "dr": "Dr",
        "prof": "Prof",
        "rev": "Rev",
        "sir": "Sir",
        "lady": "Lady",
    }
    donor_name = str(extracted.get("donor_name", "") or "")
    parts = donor_name.split()
    if not parts:
        return

    title_key = parts[0].lower().rstrip(".")
    if title_key not in normalise:
        return

    extracted["title"] = normalise[title_key]
    extracted["title_confidence"] = extracted.get("donor_name_confidence", 0.0)


def _ensure_document_ai_configured(client: Any) -> None:
    """Ensure the client has a configured Document AI processor."""
    from scans.document_ai import DocumentAIService

    if DocumentAIService.is_configured(client):
        return
    raise RuntimeError(
        f"Document AI is not configured for client '{client.name}'. "
        "Set document_ai_processor_id on the client record."
    )


def _extract_first_page(
    placeholder: Any,
    campaign: Any,
    known_urns: list[str] | None,
) -> tuple[dict[str, Any], bytes, dict[str, str] | None, Any]:
    """Download and extract OCR data for the primary page."""
    from scans.ocr import OCRExtractor

    image_bytes = download_r2_bytes(placeholder.image_path)
    qr_data = decode_qr_from_scan(placeholder, image_bytes)
    doc_ai_result = run_document_ai(placeholder, image_bytes, campaign.client)
    extracted = OCRExtractor.extract_from_document_ai(
        doc_ai_result,
        campaign,
        qr_data=qr_data,
        known_urns=known_urns,
        client=campaign.client,
    )
    return extracted, image_bytes, qr_data, doc_ai_result


def _extract_additional_pages(
    placeholder: Any,
    campaign: Any,
    page_keys: list[str],
    known_urns: list[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract OCR data for non-primary pages in a grouped document."""
    from core.services.field_normalizer import FieldNormalizer
    from scans.document_ai import DocumentAIService
    from scans.ocr import OCRExtractor

    extracted_pages: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    combined_unmapped_labels: set[str] = set(
        label
        for label in (placeholder.ocr_data or {}).get("unmapped_entity_labels", [])
        if isinstance(label, str)
    )
    for page_key in page_keys:
        try:
            page_bytes = download_r2_bytes(page_key)
            page_doc_ai = DocumentAIService.process_image_bytes(
                page_bytes,
                campaign.client,
            )
            page_unmapped_labels = FieldNormalizer.find_unmapped_labels(
                page_doc_ai.entities,
                campaign.client,
            )
            combined_unmapped_labels.update(page_unmapped_labels)
            extracted_pages.append(
                OCRExtractor.extract_from_document_ai(
                    page_doc_ai,
                    campaign,
                    qr_data=None,
                    known_urns=known_urns,
                    client=campaign.client,
                )
            )
            page_summaries.append(
                {
                    "key": page_key,
                    "confidence": page_doc_ai.confidence,
                    "entity_count": len(page_doc_ai.entities),
                    "unmapped_entity_labels": page_unmapped_labels,
                }
            )
            logger.debug(
                "Processed extra page %s for placeholder %s (confidence=%.2f)",
                page_key,
                placeholder.id,
                page_doc_ai.confidence,
            )
        except Exception as exc:
            logger.warning(
                "Failed to process extra page %s for placeholder %s: %s",
                page_key,
                placeholder.id,
                exc,
            )
    placeholder.ocr_data["unmapped_entity_labels"] = sorted(combined_unmapped_labels)
    return extracted_pages, page_summaries


def _postprocess_extracted_data(extracted: dict[str, Any]) -> None:
    """Apply common address and title enrichment after OCR extraction."""
    from core.services.postcode import PostcodesIOService
    from scans.ocr.extractor import OCRExtractor

    PostcodesIOService.enrich_address(extracted)
    for field_name in ("address_line1", "address_line2", "city"):
        value = str(extracted.get(field_name, "") or "").strip()
        if not value:
            continue
        if OCRExtractor._OPT_OUT_NOISE_PATTERN.search(value):
            extracted[field_name] = ""
    if not extracted.get("title") and extracted.get("donor_name"):
        derive_title_from_name(extracted)


def run_qr_and_match_only(
    placeholder: Any,
    campaign: Any,
    known_urns: list[str] | None,
) -> dict[str, Any]:
    """Download, QR-decode, and set URN — without invoking Document AI.

    Used for batches whose ``payment_method`` is in
    ``PAYMENT_METHODS_WITHOUT_DOCUMENT_AI`` (card, direct debit, CAF voucher,
    postal order). The form image must not be sent to Google Document AI, but
    QR decoding is PCI-safe and lets donor matching still run. Extraction
    fields are left empty so QA fills them in manually.
    """
    del campaign, known_urns  # only used by the Document AI extractor path

    image_bytes = download_r2_bytes(placeholder.image_path)
    qr_data = decode_qr_from_scan(placeholder, image_bytes)

    if qr_data is not None:
        qr_urn = str(qr_data.get("urn", "") or "").strip()
        if qr_urn:
            placeholder.urn = qr_urn

    existing_ocr_data = placeholder.ocr_data or {}
    new_ocr_data: dict[str, Any] = {
        "qr_decoded": placeholder.qr_decoded,
        "qr_kind": existing_ocr_data.get("qr_kind", ""),
        "total_pages_processed": 0,
        "document_ai_skipped": True,
    }
    qr_error = existing_ocr_data.get("qr_error", "")
    if qr_error:
        new_ocr_data["qr_error"] = qr_error
    placeholder.ocr_data = new_ocr_data
    placeholder.ocr_confidence = 0.0
    placeholder.extracted_data = {}
    return {}


def run_ocr_and_extract(
    placeholder: Any,
    campaign: Any,
    known_urns: list[str] | None,
) -> dict[str, Any]:
    """Download, QR-decode, run Document AI, and extract donation fields."""
    _ensure_document_ai_configured(campaign.client)
    extracted_page1, _image_bytes, _qr_data, _doc_ai_result = _extract_first_page(
        placeholder,
        campaign,
        known_urns,
    )

    all_page_keys = placeholder.page_keys or []
    extracted = extracted_page1
    if len(all_page_keys) > 1:
        extra_pages, page_summaries = _extract_additional_pages(
            placeholder,
            campaign,
            all_page_keys[1:],
            known_urns,
        )
        placeholder.ocr_data["extra_pages"] = page_summaries
        placeholder.ocr_data["total_pages_processed"] = 1 + len(extra_pages)
        extracted = merge_extracted_multi_page([extracted_page1, *extra_pages])

    _postprocess_extracted_data(extracted)
    placeholder.extracted_data = extracted
    extracted_urn = str(extracted.get("urn", "") or "").strip()
    urn_confidence_raw = extracted.get("urn_confidence", 0.0)
    urn_confidence = (
        float(urn_confidence_raw)
        if isinstance(urn_confidence_raw, (int, float))
        else 0.0
    )
    if extracted_urn and urn_confidence > 0.5:
        placeholder.urn = extracted_urn
    return extracted
