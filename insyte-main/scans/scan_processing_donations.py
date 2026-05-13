"""Donation-building helpers for scan processing."""

import contextlib
import re
from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from core.constants import MIN_OCR_CONFIDENCE
from scans.scan_processing_donors import (
    get_donor_match_candidates,
    get_review_metadata,
)

DATE_FALLBACK_NOTE = (
    "Donation date defaulted to today; OCR did not extract a usable date."
)

# Bank field length limits — match the Donation model column widths.
# UK sort codes are exactly 6 digits; UK domestic account numbers are exactly
# 8 digits. ``_digits_only`` truncates to these limits so malformed OCR (e.g.
# "123456789") cannot silently persist as a valid-looking 8-digit account.
_SORT_CODE_MAX_DIGITS = 6
_ACCOUNT_NUMBER_MAX_DIGITS = 8
_NON_DIGIT_PATTERN = re.compile(r"\D")


def _digits_only(value: Any, max_length: int) -> str:
    """Return ``value`` stripped to digits and truncated to ``max_length``."""
    if not value:
        return ""
    return _NON_DIGIT_PATTERN.sub("", str(value))[:max_length]


@dataclass(frozen=True)
class ParsedAmount:
    """Result of parsing an OCR-extracted donation amount.

    Attributes:
        value: The parsed Decimal value (``0.00`` when empty or unparseable).
        parse_failed: ``True`` only when a non-empty string failed to parse.
        raw: The original raw string from extracted data (stripped to ``str``).
    """

    value: Decimal
    parse_failed: bool
    raw: str


def parse_extracted_amount(extracted: dict[str, Any]) -> ParsedAmount:
    """Parse donation amount from OCR extracted data.

    Returns:
        A :class:`ParsedAmount` distinguishing an empty/zero value from a
        non-empty value that failed to parse.
    """
    raw = str(extracted.get("amount", "") or "")
    if not raw:
        return ParsedAmount(Decimal("0.00"), False, raw)
    try:
        return ParsedAmount(Decimal(raw), False, raw)
    except InvalidOperation:
        return ParsedAmount(Decimal("0.00"), True, raw)


def parse_extracted_date(extracted: dict[str, Any]) -> Any | None:
    """Parse donation date from OCR extracted data."""
    date_str = extracted.get("donation_date", "")
    if not date_str:
        return None
    try:
        from core.date_utils import parse_date

        return parse_date(date_str)
    except Exception:
        return None


def resolve_donor_source(placeholder: Any, campaign: Any) -> str:
    """Determine the donor source value for a donation."""
    if campaign.donor_source == "data_file" and placeholder.matched_data_file_donor:
        return "data_file"
    if placeholder.matched_donor or placeholder.matched_system_donor:
        return "house_file"
    return campaign.donor_source


def build_confidence_dict(extracted: dict[str, Any]) -> dict[str, float]:
    """Build a per-field confidence dict from extracted data."""
    confidence: dict[str, float] = {}
    for key, value in extracted.items():
        if key.endswith("_confidence") and isinstance(value, (int, float)):
            field_name = key.removesuffix("_confidence")
            if value > 0:
                confidence[field_name] = round(float(value), 2)
    return confidence


def parse_date_field(date_str: str) -> Any | None:
    """Parse a DD/MM/YYYY date string to a date object."""
    if not date_str:
        return None
    try:
        from core.date_utils import parse_date

        return parse_date(date_str)
    except Exception:
        return None


def parse_decimal_field(value: str) -> Decimal:
    """Parse a string to Decimal, returning 0.00 on failure."""
    if not value:
        return Decimal("0.00")
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal("0.00")


def _qa_status(review_metadata: dict[str, str], donation_model: Any) -> str:
    """Resolve QA status based on donor matching metadata."""
    if review_metadata["donor_match_status"] == "matched":
        return donation_model.QA_STATUS_PENDING
    return donation_model.QA_STATUS_FLAGGED


CRITICAL_DONATION_FIELDS: tuple[str, ...] = (
    "amount",
    "donor_urn",
    "payment_method",
    "donation_date",
)

# Subset of ``CRITICAL_DONATION_FIELDS`` that must trigger the mandatory QA
# hold (auto-approve cascade skip). These are the financial fields where a
# silent low-confidence accept would let an incorrect amount or date flow
# through to approved donations and ultimately to thank-you letters /
# payment processing. ``donor_urn`` and ``payment_method`` still flag the
# donation but do not by themselves trigger the hold — donor identity is
# resolved by other matching logic and payment method has a batch fallback.
MANDATORY_HOLD_FIELDS: frozenset[str] = frozenset({"amount", "donation_date"})

# Domain field names whose OCR extractor key differs (e.g. ``donor_urn`` is
# stored as ``urn`` in placeholder.extracted_data).
_FIELD_TO_OCR_KEY: dict[str, str] = {"donor_urn": "urn"}


def _confidence_field_requires_mandatory_hold(
    *,
    domain_field: str,
    confidence: float,
    threshold: float,
    extracted: dict[str, Any],
) -> bool:
    """Return ``True`` when low OCR confidence must block batch auto-approve.

    Campaign default ``ocr_confidence_threshold`` is 0.700 while Form Parser often
    reports 0.50-0.65 for crisply-printed handwriting/amount fields. Once the
    parsed amount/date is internally consistent, carrying a fractional point
    below the organisational threshold alone should not condemn an otherwise
    clean extraction to mandatory hold.
    """
    if threshold <= 0.0:
        return False
    if not (0.0 < confidence < threshold):
        return False

    if domain_field == "amount":
        parsed = parse_extracted_amount(extracted)
        return not (parsed.value >= Decimal("0.01") and not parsed.parse_failed)

    if domain_field == "donation_date":
        raw = str(extracted.get("donation_date", "") or "").strip()
        if raw:
            resolved = parse_date_field(raw)
            if resolved is not None:
                return False
        return True

    return True


def _low_confidence_field_records(
    placeholder: Any,
    fields: tuple[str, ...],
    threshold: float,
) -> list[dict[str, Any]]:
    """Return ``{field, confidence}`` records for low-confidence critical fields.

    A field is only flagged when the OCR extractor recorded a positive
    confidence score for it; missing or zero scores are treated as "no signal"
    so empty placeholders do not over-flag. Persisted to
    ``Donation.low_confidence_fields`` so the QA UI can surface both the
    offending field name and the score the extractor reported.
    """
    extracted = getattr(placeholder, "extracted_data", None) or {}
    records: list[dict[str, Any]] = []
    for field in fields:
        ocr_key = _FIELD_TO_OCR_KEY.get(field, field)
        confidence_raw = extracted.get(f"{ocr_key}_confidence")
        if not isinstance(confidence_raw, (int, float)):
            continue
        confidence = float(confidence_raw)
        if not _confidence_field_requires_mandatory_hold(
            domain_field=field,
            confidence=confidence,
            threshold=threshold,
            extracted=extracted,
        ):
            continue
        records.append({"field": field, "confidence": round(confidence, 3)})
    return records


def _low_confidence_fields(
    placeholder: Any,
    fields: tuple[str, ...],
    threshold: float,
) -> list[str]:
    """Return critical field names whose per-field OCR confidence is below threshold."""
    return [
        record["field"]
        for record in _low_confidence_field_records(placeholder, fields, threshold)
    ]


def _resolve_ocr_threshold(campaign: Any) -> float:
    """Return the effective per-field OCR confidence threshold for a campaign.

    Falls back to :data:`core.constants.MIN_OCR_CONFIDENCE` when the campaign
    record predates the per-campaign field (e.g. legacy fixtures or partial
    test mocks). The campaign value is stored as :class:`~decimal.Decimal` and
    converted to ``float`` so callers can stay on the existing comparison API.
    """
    raw = getattr(campaign, "ocr_confidence_threshold", None)
    if raw is None:
        return MIN_OCR_CONFIDENCE
    try:
        return float(raw)
    except TypeError, ValueError:
        return MIN_OCR_CONFIDENCE


def _resolved_payment_and_notes(
    extracted: dict[str, Any],
    campaign: Any,
    scan_batch: Any,
    placeholder: Any,
    record_type: str,
) -> tuple[str, ParsedAmount, str]:
    """Resolve payment method, parsed amount, and QA notes for a placeholder."""
    from donations.models import Donation

    is_donor_update = getattr(campaign, "scan_purpose", "donation") == "donor_update"
    if is_donor_update:
        return (
            "non_financial",
            ParsedAmount(Decimal("0.00"), False, ""),
            "",
        )

    batch_method = str(getattr(scan_batch, "payment_method", "") or "").strip()
    extracted_method = str(extracted.get("payment_method", "") or "").strip()
    valid_codes = {str(code) for code, _lbl in Donation.PAYMENT_METHOD_CHOICES}
    if extracted_method in valid_codes:
        payment_method_resolved = extracted_method
    elif batch_method:
        payment_method_resolved = batch_method
    else:
        payment_method_resolved = "non_financial"

    return (
        payment_method_resolved,
        parse_extracted_amount(extracted),
        "",
    )


def _append_qa_note(existing: str, addition: str) -> str:
    """Append a note line to an existing qa_notes string, joined by newline."""
    if not existing:
        return addition
    return f"{existing}\n{addition}"


def _donation_field_data(
    placeholder: Any,
    review_metadata: dict[str, str],
    extracted: dict[str, Any],
    campaign: Any,
) -> dict[str, Any]:
    """Build the structured field_data payload for an OCR-created donation."""
    return {
        "ocr_extracted": True,
        "ocr_confidence": placeholder.ocr_confidence,
        "scan_placeholder_id": str(placeholder.id),
        "campaign_temperature": review_metadata["campaign_temperature"],
        "record_type": review_metadata["campaign_temperature"],
        "identifier_source": review_metadata["identifier_source"],
        "donor_match_status": review_metadata["donor_match_status"],
        "exception_reason": review_metadata["exception_reason"],
        "qr_decoded": placeholder.qr_decoded,
        "scan_purpose": (
            "donor_update"
            if getattr(campaign, "scan_purpose", "donation") == "donor_update"
            else "donation"
        ),
        "confidence": build_confidence_dict(extracted),
    }


def _link_package_code(donation: Any, extracted: dict[str, Any]) -> None:
    """Link an extracted package code to the created donation."""
    pkg_code = str(extracted.get("package_code", "") or "").strip()
    if not pkg_code:
        return

    from campaigns.models import PackageCode

    with contextlib.suppress(PackageCode.DoesNotExist):
        donation.package_codes.add(PackageCode.objects.get(code=pkg_code))


def _mark_placeholder_captured(
    placeholder: Any,
    donation: Any,
    *,
    redact_bank_fields: bool = False,
) -> None:
    """Link the placeholder to the created donation and mark it captured.

    When ``redact_bank_fields`` is ``True`` the placeholder's bank PII is
    redacted in the same save so direct-debit donations produce a single
    write rather than two.
    """
    placeholder.donation = donation
    placeholder.is_captured = True
    update_fields = ["donation", "is_captured", "updated_at"]
    if redact_bank_fields and _redact_bank_fields_in_place(placeholder):
        update_fields.append("extracted_data")
    placeholder.save(update_fields=update_fields)


def _resolve_donation_date(
    extracted: dict[str, Any],
    payment_method: str,
) -> tuple[date_cls, bool]:
    """Pick the best OCR-extracted donation date, or fall back to today.

    Returns a ``(donation_date, used_fallback)`` tuple. Payment-method-specific
    fields (``cheque_date``/``postal_order_date``) win over the generic
    ``donation_date`` when present and parseable.
    """
    normalized_method = str(payment_method or "").strip()
    candidates: list[str] = []
    if normalized_method == "cheque":
        candidates.append("cheque_date")
    elif normalized_method == "postal_order":
        candidates.extend(["postal_order_date", "cheque_date"])
    candidates.append("donation_date")

    for key in candidates:
        parsed = parse_date_field(extracted.get(key, ""))
        if parsed is not None:
            return parsed, False
    return timezone.now().date(), True


def _redact_bank_fields_in_place(placeholder: Any) -> bool:
    """Replace bank PII in ``placeholder.extracted_data`` with redaction markers.

    The encrypted copy lives on the Donation; keeping a plaintext copy in the
    JSON column would defeat the encryption. We overwrite the values with
    ``"[REDACTED]"`` so audit/replay still sees that the fields *were* present
    without exposing the digits.

    Mutates ``placeholder.extracted_data`` in place. Returns ``True`` when at
    least one field was redacted, allowing the caller to fold the JSON write
    into a sibling ``placeholder.save()`` call instead of issuing a second
    save (which would generate a duplicate audit row per direct-debit
    donation).
    """
    extracted = placeholder.extracted_data or {}
    redacted = False
    for key in ("sort_code", "account_number"):
        if extracted.get(key):
            extracted[key] = "[REDACTED]"
            redacted = True
    if redacted:
        placeholder.extracted_data = extracted
    return redacted


def _payment_specific_fields(
    extracted: dict[str, Any],
    payment_method: str,
) -> dict[str, Any]:
    """Return payment-specific donation fields for the resolved payment method."""
    normalized_method = str(payment_method or "").strip()
    generic_payment_date = parse_date_field(extracted.get("donation_date", ""))

    fields: dict[str, Any] = {
        "cheque_number": "",
        "cheque_date": None,
        "card_holder_name": "",
        "card_last_four": "",
        "card_expiry_date": "",
        "caf_voucher_number": "",
        "caf_amount": Decimal("0.00"),
        "postal_order_number": "",
        "postal_order_date": None,
        "sort_code": "",
        "account_number": "",
    }

    if normalized_method in {"card", "direct_debit"}:
        fields.update(
            {
                "card_holder_name": extracted.get("card_holder_name", ""),
                "card_last_four": extracted.get("card_last_four", "")[:4],
                "card_expiry_date": extracted.get("card_expiry_date", ""),
            }
        )
        if normalized_method == "direct_debit":
            fields.update(
                {
                    "sort_code": _digits_only(
                        extracted.get("sort_code", ""), _SORT_CODE_MAX_DIGITS
                    ),
                    "account_number": _digits_only(
                        extracted.get("account_number", ""),
                        _ACCOUNT_NUMBER_MAX_DIGITS,
                    ),
                }
            )
    elif normalized_method == "cheque":
        fields.update(
            {
                "cheque_number": extracted.get("cheque_number", ""),
                "cheque_date": parse_date_field(extracted.get("cheque_date", ""))
                or generic_payment_date,
            }
        )
    elif normalized_method == "caf":
        fields.update(
            {
                "caf_voucher_number": extracted.get("caf_voucher_number", ""),
                "caf_amount": parse_decimal_field(extracted.get("caf_amount", "")),
            }
        )
    elif normalized_method == "postal_order":
        fields.update(
            {
                "postal_order_number": extracted.get("postal_order_number", "")
                or extracted.get("cheque_number", ""),
                "postal_order_date": parse_date_field(
                    extracted.get("postal_order_date", "")
                )
                or parse_date_field(extracted.get("cheque_date", ""))
                or generic_payment_date,
            }
        )

    return fields


def create_donation_from_placeholder(
    placeholder: Any,
    campaign: Any,
    donation_batch: Any,
    scan_batch: Any,
) -> Any | None:
    """Create a single Donation from a processed ScanPlaceholder.

    Returns ``None`` for donor-update campaigns (no financial record is created).
    """
    from donations.models import Donation

    # Issue 7: donor-update batches update the donor record only — no donation needed.
    if getattr(campaign, "scan_purpose", "donation") == "donor_update":
        return None

    extracted = placeholder.extracted_data or {}
    review_metadata = get_review_metadata(placeholder, campaign)
    record_type = review_metadata["campaign_temperature"].title()
    payment_method, parsed_amount, qa_notes = _resolved_payment_and_notes(
        extracted,
        campaign,
        scan_batch,
        placeholder,
        record_type,
    )
    payment_specific_fields = _payment_specific_fields(extracted, payment_method)
    resolved_donation_date, date_used_fallback = _resolve_donation_date(
        extracted, payment_method
    )
    # Issue 9: flag low-confidence extractions for manual review.
    confidence = float(getattr(placeholder, "ocr_confidence", 0.0) or 0.0)
    computed_qa_status = _qa_status(review_metadata, Donation)
    if confidence < MIN_OCR_CONFIDENCE:
        computed_qa_status = Donation.QA_STATUS_FLAGGED
    # Issue 23: per-campaign threshold + structured low-confidence record so
    # the QA cascade auto-approve can skip these donations and the UI can
    # surface which fields need human verification.
    field_threshold = _resolve_ocr_threshold(campaign)
    low_field_records = _low_confidence_field_records(
        placeholder, CRITICAL_DONATION_FIELDS, field_threshold
    )
    low_fields = [record["field"] for record in low_field_records]
    if low_fields:
        computed_qa_status = Donation.QA_STATUS_FLAGGED
        low_field_note = f"Low OCR confidence on: {', '.join(low_fields)}"
        qa_notes = (
            f"{qa_notes}\n{low_field_note}".strip() if qa_notes else low_field_note
        )

    if date_used_fallback:
        computed_qa_status = Donation.QA_STATUS_FLAGGED
        qa_notes = (
            f"{qa_notes}\n{DATE_FALLBACK_NOTE}" if qa_notes else DATE_FALLBACK_NOTE
        )

    if parsed_amount.parse_failed:
        computed_qa_status = Donation.QA_STATUS_FLAGGED
        qa_notes = _append_qa_note(
            qa_notes,
            f"OCR amount unparseable: '{parsed_amount.raw}' — please verify against the form.",
        )

    # Borderline donor matches (Jaro-Winkler 0.85-0.95) are NOT auto-linked;
    # the candidate payload rides along on the donation so QA can
    # disambiguate, and we force the QA status to flagged so the donation
    # cannot be silently approved without a reviewer picking the right
    # donor. Prefer the structured
    # ``ScanPlaceholder.donor_match_candidates`` payload (list of dicts
    # with ``urn``/``score``/``name``/``system_donor_id``); fall back to
    # the legacy bare PK list on ``ocr_data`` only when the structured
    # field is absent (placeholders predating the field migration).
    structured_candidates = list(
        getattr(placeholder, "donor_match_candidates", []) or []
    )
    if structured_candidates:
        donation_candidates: list[Any] = structured_candidates
    else:
        donation_candidates = list(get_donor_match_candidates(placeholder))
    if donation_candidates:
        computed_qa_status = Donation.QA_STATUS_FLAGGED
        qa_notes = _append_qa_note(
            qa_notes,
            "Borderline donor name match — pick the correct donor or create a new one.",
        )

    resolved_amount = parsed_amount.value
    # Only persist records for fields whose low-confidence reading must block
    # auto-approve (amount, donation_date). Other low-confidence flags still
    # appear in qa_notes but should not lock down the cascade — that prevents
    # routine "low URN confidence" reads (which donor matching often resolves)
    # from forcing per-donation review on every batch.
    hold_records = [
        record
        for record in low_field_records
        if record["field"] in MANDATORY_HOLD_FIELDS
    ]
    donation = Donation.objects.create(
        campaign=campaign,
        batch=donation_batch,
        donor_source=resolve_donor_source(placeholder, campaign),
        donor=placeholder.matched_donor,
        data_file_donor=placeholder.matched_data_file_donor,
        system_donor=placeholder.matched_system_donor,
        amount=resolved_amount if resolved_amount > 0 else Decimal("0.00"),
        currency="GBP",
        payment_method=payment_method,
        donation_date=resolved_donation_date,
        gift_aid=bool(extracted.get("gift_aid", False)),
        cheque_number=payment_specific_fields["cheque_number"],
        cheque_date=payment_specific_fields["cheque_date"],
        card_holder_name=payment_specific_fields["card_holder_name"],
        card_last_four=payment_specific_fields["card_last_four"],
        card_expiry_date=payment_specific_fields["card_expiry_date"],
        caf_voucher_number=payment_specific_fields["caf_voucher_number"],
        caf_amount=payment_specific_fields["caf_amount"],
        postal_order_number=payment_specific_fields["postal_order_number"],
        postal_order_date=payment_specific_fields["postal_order_date"],
        sort_code=payment_specific_fields["sort_code"],
        account_number=payment_specific_fields["account_number"],
        qa_status=computed_qa_status,
        qa_notes=qa_notes,
        low_confidence_fields=hold_records,
        donor_match_candidates=donation_candidates,
        filled_by=scan_batch.created_by,
        field_data=_donation_field_data(
            placeholder, review_metadata, extracted, campaign
        ),
    )
    _link_package_code(donation, extracted)
    _mark_placeholder_captured(
        placeholder,
        donation,
        redact_bank_fields=(payment_method == "direct_debit"),
    )
    return donation


def _processed_placeholders(scan_batch: Any) -> Any:
    """Return placeholders eligible for donation creation."""
    from scans.models import ScanPlaceholder

    return ScanPlaceholder.objects.filter(
        batch=scan_batch,
        ocr_status__in=[
            ScanPlaceholder.OCR_STATUS_MATCHED,
            ScanPlaceholder.OCR_STATUS_UNMATCHED,
            ScanPlaceholder.OCR_STATUS_COMPLETED,
            ScanPlaceholder.OCR_STATUS_SKIPPED,
        ],
    ).select_related("matched_donor", "matched_data_file_donor", "matched_system_donor")


def create_donation_batch(scan_batch: Any) -> Any | None:
    """Create or update a DonationBatch from processed ScanPlaceholders.

    Idempotent on ``(campaign, batch_name)``: if a DonationBatch already
    exists (e.g. a chord callback re-delivery, a manual finalize re-run, or
    the chord-error fallback firing after a partial first finalize), this
    function links any newly-eligible placeholders to it instead of trying
    to create a duplicate row that would violate
    ``donationbatch_campaign_batch_name_uniq``.

    Race-safe: two concurrent finalize calls (chord body + explicit
    fallback dispatch) may both observe "no existing batch" and race to
    create. The unique constraint guarantees at most one winner, and the
    loser catches ``IntegrityError`` to look the winner's row back up — so
    both calls converge on the same DonationBatch with linked donations.

    Returns ``None`` for donor-update campaigns (no financial records created)
    or when no eligible placeholders exist and no batch already exists.
    """
    from django.db import IntegrityError

    from donations.models import Donation, DonationBatch

    # Issue 7: donor-update campaigns update donor records only — no DonationBatch needed.
    if getattr(scan_batch.campaign, "scan_purpose", "donation") == "donor_update":
        return None

    placeholders = _processed_placeholders(scan_batch)
    pre_existing = DonationBatch.objects.filter(
        campaign=scan_batch.campaign,
        batch_name=scan_batch.batch_name,
    ).first()

    if pre_existing is None and not placeholders.exists():
        return None

    with transaction.atomic():
        if pre_existing is not None:
            donation_batch = pre_existing
        else:
            try:
                with transaction.atomic():
                    donation_batch = DonationBatch.objects.create(
                        campaign=scan_batch.campaign,
                        batch_name=scan_batch.batch_name,
                        default_payment_method=scan_batch.payment_method,
                        default_currency="GBP",
                        created_by=scan_batch.created_by,
                        status=DonationBatch.STATUS_PENDING_QA,
                    )
            except IntegrityError:
                # A concurrent finalize beat us to the unique constraint
                # — fetch the winner's row and continue along the
                # link-existing path. The savepoint above is rolled back
                # so the outer transaction stays usable.
                winner = DonationBatch.objects.filter(
                    campaign=scan_batch.campaign,
                    batch_name=scan_batch.batch_name,
                ).first()
                if winner is None:
                    raise  # IntegrityError without a row — unrecoverable.
                donation_batch = winner
        # Re-running finalize must not create duplicate Donations: skip any
        # placeholder that is already linked to a Donation row.
        unlinked = placeholders.filter(donation__isnull=True)
        for placeholder in unlinked:
            create_donation_from_placeholder(
                placeholder,
                scan_batch.campaign,
                donation_batch,
                scan_batch,
            )

        # Recompute totals from authoritative row counts so prior drift can't
        # compound across re-runs.
        donation_batch.total_donations = Donation.objects.filter(
            batch=donation_batch
        ).count()
        donation_batch.total_amount = Donation.objects.filter(
            batch=donation_batch
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")
        donation_batch.save(
            update_fields=["total_donations", "total_amount", "updated_at"]
        )
    return donation_batch
