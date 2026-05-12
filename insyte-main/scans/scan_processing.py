"""Scan processing service for automated OCR batch creation.

All heavy logic lives in focused sub-modules:
  scan_processing_r2.py, scan_processing_ocr.py,
  scan_processing_donors.py, scan_processing_donations.py
"""

import logging
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from scans.batch_naming import derive_batch_identity
from scans.scan_processing_donations import (
    build_confidence_dict,
    create_donation_batch,
    create_donation_from_placeholder,
    parse_date_field,
    parse_decimal_field,
    parse_extracted_amount,
    parse_extracted_date,
    resolve_donor_source,
)
from scans.scan_processing_donors import (
    apply_donor_match,
    create_new_placeholder_donor,
    describe_exception_reason,
    get_review_metadata,
    set_review_metadata,
    split_donor_name,
    sync_donation_donor,
)
from scans.scan_processing_ocr import (
    decode_qr_from_scan,
    derive_title_from_name,
    merge_extracted_multi_page,
    run_document_ai,
    run_ocr_and_extract,
    run_qr_and_match_only,
)
from scans.scan_processing_r2 import build_placeholder, group_keys_for_layout

logger = logging.getLogger(__name__)


class ScanProcessingService:
    """End-to-end scan processing facade: upload → OCR → donor match → batch."""

    @staticmethod
    def create_scan_batch_from_r2(
        campaign_id: str,
        r2_keys: list[str],
        payment_method: str,
        scan_form_type: str,
        user: Any | None = None,
        batch_name: str = "",
    ) -> Any:
        """Create a ScanBatch with ScanPlaceholders from R2 keys."""
        from scans.models import ScanBatch

        if not r2_keys:
            raise ValueError("No R2 keys provided for scan batch.")

        campaign = _load_campaign(campaign_id)
        batch_identity = derive_batch_identity(r2_keys, batch_name)
        resolved_name = batch_identity.batch_name
        _validate_batch_uniqueness(campaign, resolved_name)
        ScanBatch.validate_batch_request(payment_method, scan_form_type)
        key_groups, donor_count = group_keys_for_layout(
            campaign_id, r2_keys, scan_form_type
        )
        _check_max_batch_size(donor_count)

        scan_batch = _create_batch_records(
            campaign=campaign,
            batch_name=resolved_name,
            source_filename=batch_identity.source_filename,
            payment_method=payment_method,
            scan_form_type=scan_form_type,
            donor_count=donor_count,
            key_groups=key_groups,
            user=user,
        )
        logger.info(
            "Created scan batch '%s' with %d donor documents for campaign %s "
            "(scan_form_type=%s, scan_purpose=%s)",
            resolved_name,
            donor_count,
            campaign_id,
            scan_form_type,
            campaign.scan_purpose,
        )
        return scan_batch

    # ── Backward-compatible delegates to sub-module functions ──────────────

    @staticmethod
    def _decode_qr_from_scan(
        placeholder: Any,
        image_bytes: bytes,
    ) -> dict[str, str] | None:
        """Delegate to scan_processing_ocr.decode_qr_from_scan."""
        return decode_qr_from_scan(placeholder, image_bytes)

    @staticmethod
    def _run_document_ai(
        placeholder: Any,
        image_bytes: bytes,
        client: Any,
    ) -> Any:
        """Delegate to scan_processing_ocr.run_document_ai."""
        return run_document_ai(placeholder, image_bytes, client)

    @staticmethod
    def _merge_extracted_multi_page(
        page_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Delegate to scan_processing_ocr.merge_extracted_multi_page."""
        return merge_extracted_multi_page(page_results)

    @staticmethod
    def _run_ocr_and_extract(
        placeholder: Any,
        campaign: Any,
        known_urns: list[str] | None,
    ) -> dict[str, Any]:
        """Delegate to scan_processing_ocr.run_ocr_and_extract."""
        return run_ocr_and_extract(placeholder, campaign, known_urns)

    @staticmethod
    def _derive_title_from_name(extracted: dict[str, Any]) -> None:
        """Delegate to scan_processing_ocr.derive_title_from_name."""
        derive_title_from_name(extracted)

    @staticmethod
    def _split_donor_name(full_name: str) -> tuple[str, str]:
        """Delegate to scan_processing_donors.split_donor_name."""
        return split_donor_name(full_name)

    @staticmethod
    def _create_new_placeholder_donor(
        placeholder: Any,
        campaign: Any,
    ) -> Any:
        """Delegate to scan_processing_donors.create_new_placeholder_donor."""
        return create_new_placeholder_donor(placeholder, campaign)

    @staticmethod
    def _sync_donation_donor(placeholder: Any, donor: Any) -> None:
        """Delegate to scan_processing_donors.sync_donation_donor."""
        sync_donation_donor(placeholder, donor)

    @staticmethod
    def _apply_donor_match(
        placeholder: Any,
        campaign: Any,
    ) -> dict[str, Any]:
        """Delegate to scan_processing_donors.apply_donor_match."""
        return apply_donor_match(placeholder, campaign)

    @staticmethod
    def _set_review_metadata(
        placeholder: Any,
        campaign: Any,
        donor_match_status: str,
        exception_reason: str,
    ) -> None:
        """Delegate to scan_processing_donors.set_review_metadata."""
        set_review_metadata(placeholder, campaign, donor_match_status, exception_reason)

    @staticmethod
    def _get_review_metadata(
        placeholder: Any,
        campaign: Any,
    ) -> dict[str, str]:
        """Delegate to scan_processing_donors.get_review_metadata."""
        return get_review_metadata(placeholder, campaign)

    @staticmethod
    def _describe_exception_reason(exception_reason: str) -> str:
        """Delegate to scan_processing_donors.describe_exception_reason."""
        return describe_exception_reason(exception_reason)

    @staticmethod
    def _parse_extracted_amount(extracted: dict[str, Any]) -> Any:
        """Delegate to scan_processing_donations.parse_extracted_amount.

        Returns a :class:`ParsedAmount` carrying the parsed value plus a
        ``parse_failed`` flag and the original raw string.
        """
        return parse_extracted_amount(extracted)

    @staticmethod
    def _parse_extracted_date(extracted: dict[str, Any]) -> Any | None:
        """Delegate to scan_processing_donations.parse_extracted_date."""
        return parse_extracted_date(extracted)

    @staticmethod
    def _parse_date_field(date_str: str) -> Any | None:
        """Delegate to scan_processing_donations.parse_date_field."""
        return parse_date_field(date_str)

    @staticmethod
    def _parse_decimal_field(value: str) -> Any:
        """Delegate to scan_processing_donations.parse_decimal_field."""
        return parse_decimal_field(value)

    @staticmethod
    def _resolve_donor_source(placeholder: Any, campaign: Any) -> str:
        """Delegate to scan_processing_donations.resolve_donor_source."""
        return resolve_donor_source(placeholder, campaign)

    @staticmethod
    def _build_confidence_dict(extracted: dict[str, Any]) -> dict[str, float]:
        """Delegate to scan_processing_donations.build_confidence_dict."""
        return build_confidence_dict(extracted)

    @staticmethod
    def _create_donation_from_placeholder(
        placeholder: Any,
        campaign: Any,
        donation_batch: Any,
        scan_batch: Any,
    ) -> Any:
        """Delegate to scan_processing_donations.create_donation_from_placeholder."""
        return create_donation_from_placeholder(
            placeholder, campaign, donation_batch, scan_batch
        )

    @staticmethod
    def _create_donation_batch(scan_batch: Any) -> Any | None:
        """Delegate to scan_processing_donations.create_donation_batch."""
        return create_donation_batch(scan_batch)

    # ── Public orchestration methods ───────────────────────────────────────

    @staticmethod
    def process_single_scan(
        placeholder_id: str,
        known_urns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Process a single ScanPlaceholder through OCR and donor matching.

        For batches whose payment_method is in
        ``PAYMENT_METHODS_WITHOUT_DOCUMENT_AI``, Document AI extraction is
        skipped; only QR decoding and donor matching run so that blank
        Donation records can still be created for QA to fill in.

        Transient errors (Document AI exhausted retries, R2 5xx, network
        timeouts) re-raise as :class:`TransientOCRError` so the per-scan
        Celery task's ``max_retries`` covers minutes-scale outages. Permanent
        failures mark the placeholder ``OCR_STATUS_FAILED`` and return a
        result dict so the chord can finalise the batch.
        """
        from scans.document_ai import TransientOCRError
        from scans.models import ScanPlaceholder
        from scans.scan_constants import PAYMENT_METHODS_WITHOUT_DOCUMENT_AI

        try:
            placeholder = ScanPlaceholder.objects.select_related(
                "batch__campaign__client",
                "batch__campaign",
            ).get(id=placeholder_id)
        except ScanPlaceholder.DoesNotExist:
            raise ValueError(f"ScanPlaceholder not found: {placeholder_id}") from None

        campaign = placeholder.batch.campaign
        skip_document_ai = (
            placeholder.batch.payment_method in PAYMENT_METHODS_WITHOUT_DOCUMENT_AI
        )
        placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_PROCESSING
        placeholder.save(update_fields=["ocr_status", "updated_at"])

        try:
            with transaction.atomic():
                if skip_document_ai:
                    extracted = run_qr_and_match_only(placeholder, campaign, known_urns)
                else:
                    extracted = run_ocr_and_extract(placeholder, campaign, known_urns)
                match_result = apply_donor_match(placeholder, campaign)
                if (
                    skip_document_ai
                    and placeholder.ocr_status != ScanPlaceholder.OCR_STATUS_MATCHED
                ):
                    # Preserve apply_donor_match's MATCHED transition when it fires;
                    # otherwise settle on SKIPPED so the placeholder is clearly
                    # distinct from UNMATCHED / COMPLETED in reports and filters.
                    placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_SKIPPED
                placeholder.save()

                if (new_donor := match_result.get("donor")) is not None:
                    sync_donation_donor(placeholder, new_donor)

            logger.info(
                "Processed scan %s: URN=%s, status=%s, amount=%s, skip_document_ai=%s",
                placeholder_id,
                placeholder.urn,
                placeholder.ocr_status,
                extracted.get("amount", ""),
                skip_document_ai,
            )
            return _scan_result_dict(placeholder, match_result, extracted)
        except TransientOCRError:
            # Surface transient errors so the per-scan Celery task retries.
            # The placeholder stays in OCR_STATUS_PROCESSING; the next attempt
            # will overwrite it. If retries are exhausted, the task handler
            # marks it FAILED before raising the final exception.
            logger.warning(
                "Transient OCR failure for scan %s — raising for Celery retry",
                placeholder_id,
            )
            raise
        except Exception as exc:
            return _handle_scan_failure(placeholder, exc, placeholder_id)

    @staticmethod
    def prepare_scan_batch(
        scan_batch_id: str,
    ) -> tuple[Any, list[str], list[str]]:
        """Prepare a scan batch for fan-out processing.

        Fetches the batch, marks it as PROCESSING, loads the campaign URN
        list once (to avoid N database queries across parallel tasks), and
        returns the list of pending placeholder IDs.

        Args:
            scan_batch_id: UUID of the ScanBatch to prepare.

        Returns:
            Tuple of (scan_batch, known_urns, placeholder_ids).

        Raises:
            ValueError: If the ScanBatch does not exist.
        """
        from scans.models import ScanBatch, ScanPlaceholder
        from scans.ocr import donor_matching

        try:
            scan_batch = ScanBatch.objects.select_related(
                "campaign__client", "campaign"
            ).get(id=scan_batch_id)
        except ScanBatch.DoesNotExist:
            raise ValueError(f"ScanBatch not found: {scan_batch_id}") from None

        scan_batch.status = ScanBatch.STATUS_PROCESSING
        scan_batch.save(update_fields=["status", "updated_at"])

        known_urns = donor_matching.load_campaign_urns(scan_batch.campaign)
        placeholder_ids = list(
            ScanPlaceholder.objects.filter(
                batch=scan_batch,
                ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
            ).values_list("id", flat=True)
        )
        return scan_batch, known_urns, [str(ph_id) for ph_id in placeholder_ids]

    @staticmethod
    def finalize_scan_batch(
        scan_batch_id: str,
        total: int,
        matched: int,
        failed: int,
    ) -> dict[str, Any]:
        """Finalise a scan batch after all individual scans have completed.

        Creates the DonationBatch (idempotent on ``(campaign, batch_name)``),
        updates ScanBatch status and counters. Safe to invoke twice for the
        same scan batch: the second call links any newly-processed
        placeholders to the existing DonationBatch and refreshes counters.
        Both writes happen inside a single ``transaction.atomic`` so a crash
        between donation-batch creation and scan-batch save can't leave
        partial state.

        Args:
            scan_batch_id: UUID of the ScanBatch.
            total: Total scans processed.
            matched: Scans matched to a donor.
            failed: Scans that failed OCR or matching.

        Returns:
            dict with processing summary.

        Raises:
            ValueError: If the ScanBatch does not exist.
        """
        from scans.models import ScanBatch

        with transaction.atomic():
            try:
                scan_batch = ScanBatch.objects.select_related(
                    "campaign__client", "campaign"
                ).get(id=scan_batch_id)
            except ScanBatch.DoesNotExist:
                raise ValueError(f"ScanBatch not found: {scan_batch_id}") from None

            donation_batch = create_donation_batch(scan_batch)
            scan_batch.status = ScanBatch.final_status_for_outcomes(
                total=total,
                matched=matched,
                failed=failed,
            )
            scan_batch.donation_batch = donation_batch
            scan_batch.processed_scans = total
            scan_batch.matched_scans = matched
            scan_batch.save(
                update_fields=[
                    "status",
                    "donation_batch",
                    "processed_scans",
                    "matched_scans",
                    "updated_at",
                ]
            )

        logger.info(
            "Scan batch '%s' finalised: %d total, %d matched, %d failed",
            scan_batch.batch_name,
            total,
            matched,
            failed,
        )
        return {
            "scan_batch_id": scan_batch_id,
            "total": total,
            "processed": total,
            "matched": matched,
            "failed": failed,
            "donation_batch_id": donation_batch.id if donation_batch else None,
        }

    @staticmethod
    def get_scan_batch_status(scan_batch_id: str) -> dict[str, Any]:
        """Get the current status of a scan batch processing job."""
        from scans.models import ScanBatch, ScanPlaceholder

        try:
            batch = ScanBatch.objects.get(id=scan_batch_id)
        except ScanBatch.DoesNotExist:
            return {"error": "Scan batch not found"}

        status_counts: dict[str, int] = {
            status_key: ScanPlaceholder.objects.filter(
                batch=batch, ocr_status=status_key
            ).count()
            for status_key, _ in ScanPlaceholder.OCR_STATUS_CHOICES
        }
        return {
            "scan_batch_id": scan_batch_id,
            "batch_name": batch.batch_name,
            "status": batch.status,
            "total_scans": batch.total_scans,
            "processed_scans": batch.processed_scans,
            "matched_scans": batch.matched_scans,
            "progress_pct": batch.progress_pct,
            "donation_batch_id": (
                batch.donation_batch_id if batch.donation_batch_id else None
            ),
            "placeholder_statuses": status_counts,
        }


# ── Module-level helpers ───────────────────────────────────────────────────


def _load_campaign(campaign_id: str) -> Any:
    """Load a Campaign by id or raise a descriptive ValueError."""
    from campaigns.models import Campaign

    try:
        return Campaign.objects.select_related("client").get(id=campaign_id)
    except Campaign.DoesNotExist:
        raise ValueError(f"Campaign not found: {campaign_id}") from None


def _check_max_batch_size(donor_count: int) -> None:
    """Raise ValueError when donor_count exceeds SCAN_BATCH_MAX_SIZE."""
    max_size = int(getattr(settings, "SCAN_BATCH_MAX_SIZE", 30))
    if max_size > 0 and donor_count > max_size:
        raise ValueError(
            f"Physical batch exceeds the maximum allowed size of {max_size} donor forms."
        )


def _validate_batch_uniqueness(campaign: Any, batch_name: str) -> None:
    """Raise ValueError if a ScanBatch or DonationBatch with this name exists."""
    from donations.models import DonationBatch
    from scans.models import ScanBatch

    if ScanBatch.objects.filter(campaign=campaign, batch_name=batch_name).exists():
        raise ValueError(
            f"Batch '{batch_name}' already exists for campaign {campaign.name}."
        )
    if DonationBatch.objects.filter(campaign=campaign, batch_name=batch_name).exists():
        raise ValueError(
            f"Batch '{batch_name}' already exists for campaign {campaign.name}."
        )


def _create_batch_records(
    campaign: Any,
    batch_name: str,
    source_filename: str,
    payment_method: str,
    scan_form_type: str,
    donor_count: int,
    key_groups: list[list[str]],
    user: Any | None,
) -> Any:
    """Create ScanBatch and ScanPlaceholder records atomically.

    The unique constraint ``scanbatch_campaign_batch_name_uniq`` is the
    race-safe check; ``_validate_batch_uniqueness`` only provides an
    earlier friendly error and can be lost to a race. The resulting
    ``IntegrityError`` is translated so callers keep catching ``ValueError``.
    """
    from scans.models import ScanBatch, ScanPlaceholder

    try:
        with transaction.atomic():
            scan_batch = ScanBatch.objects.create(
                campaign=campaign,
                batch_name=batch_name,
                source_filename=source_filename,
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
    except IntegrityError as exc:
        raise ValueError(
            f"Batch '{batch_name}' already exists for campaign {campaign.name}."
        ) from exc
    return scan_batch


def _scan_result_dict(
    placeholder: Any,
    match_result: dict[str, Any],
    extracted: dict[str, Any],
) -> dict[str, Any]:
    """Build the success result dict for process_single_scan."""
    ocr_data = placeholder.ocr_data or {}
    return {
        "placeholder_id": str(placeholder.id),
        "urn": placeholder.urn,
        "ocr_status": placeholder.ocr_status,
        "donor_matched": match_result["source"] != "not_found",
        "donor_source": match_result["source"],
        "extracted_amount": extracted.get("amount", ""),
        "extracted_gift_aid": extracted.get("gift_aid"),
        "qr_decoded": placeholder.qr_decoded,
        "identifier_source": ocr_data.get("identifier_source", ""),
        "donor_match_status": ocr_data.get("donor_match_status", ""),
        "exception_reason": ocr_data.get("exception_reason", ""),
    }


def _handle_scan_failure(
    placeholder: Any,
    exc: Exception,
    placeholder_id: str,
) -> dict[str, Any]:
    """Handle a scan processing failure and return an error result dict."""
    from scans.models import ScanPlaceholder

    placeholder.ocr_status = ScanPlaceholder.OCR_STATUS_FAILED
    placeholder.processing_error = str(exc)[:1000]
    placeholder.save(update_fields=["ocr_status", "processing_error", "updated_at"])
    logger.exception("Failed to process scan %s", placeholder_id)
    return {
        "placeholder_id": str(placeholder.id),
        "urn": placeholder.urn,
        "ocr_status": ScanPlaceholder.OCR_STATUS_FAILED,
        "error": str(exc)[:500],
    }
