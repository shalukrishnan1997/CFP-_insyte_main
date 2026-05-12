"""Scan models for the donation management system."""

import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models

from scans.scan_constants import (
    PAYMENT_METHOD_CHOICES,
    PAYMENT_METHOD_LABELS,
    PAYMENT_METHOD_TO_SCAN_FORM_TYPES,
    SCAN_FORM_TYPE_CHOICES,
    SCAN_FORM_TYPE_DUPLEX,
    SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT,
    SCAN_FORM_TYPE_MIXED_MAIL,
    SCAN_FORM_TYPE_PAGES,
    SCAN_FORM_TYPE_SIMPLEX,
    SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT,
    _coerce_scan_request_bool,
    _scan_batch_layout_compatibility_q,
)


class ScanBatch(models.Model):
    """Groups a physical batch of scans together.

    Strictly mapped to one payment method as per high-efficiency requirements.

    Attributes:
        id: UUID primary key.
        campaign: Foreign key to Campaign model.
        batch_name: Name for this scan batch.
        payment_method: Payment method shared by all scans in this batch.
        created_by: User who created this batch.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_PARTIALLY_COMPLETED = "partially_completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_PARTIALLY_COMPLETED, "Partially Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    SCAN_FORM_TYPE_SIMPLEX = SCAN_FORM_TYPE_SIMPLEX
    SCAN_FORM_TYPE_DUPLEX = SCAN_FORM_TYPE_DUPLEX
    SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT = SCAN_FORM_TYPE_SIMPLEX_WITH_PAYMENT
    SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT = SCAN_FORM_TYPE_DUPLEX_WITH_PAYMENT
    SCAN_FORM_TYPE_MIXED_MAIL = SCAN_FORM_TYPE_MIXED_MAIL
    SCAN_FORM_TYPE_CHOICES = SCAN_FORM_TYPE_CHOICES

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT (audit 2026-05-02 §4.1): scan batches retain financial-record
    # links and original PDF references — losing them on a campaign delete
    # would break audit trails and R2 cleanup.
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, related_name="scan_batches"
    )
    batch_name = models.CharField(
        max_length=255,
        help_text="Canonical physical batch identifier derived from the upload filename",
    )
    source_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Original uploaded PDF filename for this physical batch",
    )
    payment_method = models.CharField(
        max_length=30,
        choices=PAYMENT_METHOD_CHOICES,
        help_text="All scans in this batch share this payment method",
    )
    scan_form_type = models.CharField(
        max_length=30,
        choices=SCAN_FORM_TYPE_CHOICES,
        default=SCAN_FORM_TYPE_SIMPLEX,
        db_index=True,
        help_text=(
            "Physical layout of donor documents in this batch. "
            "Simplex/Duplex are valid for card, direct debit, cash, and "
            "non-financial batches. Payment-doc layouts are valid for cheque, "
            "voucher, and postal order batches."
        ),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="OCR processing status for this scan batch",
    )
    donation_batch = models.OneToOneField(
        "donations.DonationBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_batch",
        help_text="Auto-created DonationBatch from OCR results",
    )
    total_scans = models.PositiveIntegerField(
        default=0, help_text="Total number of scans in this batch"
    )
    processed_scans = models.PositiveIntegerField(
        default=0, help_text="Number of scans processed by OCR"
    )
    matched_scans = models.PositiveIntegerField(
        default=0, help_text="Number of scans matched to a donor"
    )
    processed_placeholder_ids = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Idempotency keys (ScanPlaceholder UUIDs as strings) for which a "
            "progress increment has already been applied. Prevents Celery retries "
            "of process_single_scan_task from double-counting the same scan."
        ),
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Error description when this batch failed to process",
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_scan_batches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scan Batch"
        verbose_name_plural = "Scan Batches"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["campaign", "status"], name="scanbatch_camp_stat_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "batch_name"],
                name="scanbatch_campaign_batch_name_uniq",
            ),
            models.CheckConstraint(
                condition=_scan_batch_layout_compatibility_q(),
                name="scanbatch_pay_method_form_type_compat",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the scan batch.

        Returns:
            str: Batch name with payment method display.
        """
        return f"{self.batch_name} ({self.get_payment_method_display()})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate scan-batch invariants before saving.

        Args:
            *args: Positional arguments forwarded to ``models.Model.save``.
            **kwargs: Keyword arguments forwarded to ``models.Model.save``.
        """
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Validate the selected layout for this batch's payment method.

        Raises:
            ValidationError: If the selected scan_form_type is incompatible.
        """
        error = self.batch_request_error(
            self.payment_method,
            self.scan_form_type,
            missing_payment_method_message="payment_method is required",
            missing_scan_form_type_message="scan_form_type is required",
            invalid_payment_method_message="Invalid payment method '{payment_method_display}'.",
            invalid_layout_message=(
                "{payment_method_display} batches must use one of: {allowed_labels}."
            ),
        )
        if error is not None:
            error_field = "payment_method"
            if self.allowed_scan_form_types_for_payment_method(
                self.payment_method.strip()
            ):
                error_field = "scan_form_type"
            raise ValidationError({error_field: error})

    @staticmethod
    def _error_for_unknown_payment_method(
        normalized_pm: str,
        normalized_sft: str,
        pm_display: str,
        message_template: str,
    ) -> str:
        """Build error message for an unrecognized payment method."""
        return message_template.format(
            payment_method=normalized_pm,
            payment_method_display=pm_display,
            scan_form_type=normalized_sft,
            allowed_labels="",
        )

    @classmethod
    def _error_for_incompatible_layout(
        cls,
        normalized_pm: str,
        normalized_sft: str,
        pm_display: str,
        message_template: str,
    ) -> str:
        """Build error message for an incompatible scan form layout."""
        allowed_labels = ", ".join(
            label
            for _value, label in cls.allowed_scan_form_type_choices_for_payment_method(
                normalized_pm
            )
        )
        return message_template.format(
            payment_method=normalized_pm,
            payment_method_display=pm_display,
            scan_form_type=normalized_sft,
            allowed_labels=allowed_labels,
        )

    @classmethod
    def batch_request_error(
        cls,
        payment_method: str,
        scan_form_type: str,
        *,
        missing_payment_method_message: str = "payment_method is required",
        missing_scan_form_type_message: str = "scan_form_type is required",
        invalid_payment_method_message: str = "Invalid payment_method: {payment_method}",
        invalid_layout_message: str = (
            "Invalid form layout '{scan_form_type}' for payment method "
            "'{payment_method}'. Allowed values: {allowed_labels}."
        ),
    ) -> str | None:
        """Return a validation message for payment/layout compatibility.

        Args:
            payment_method: Batch payment method value.
            scan_form_type: Batch scan form layout value.
            missing_payment_method_message: Message when payment method is blank.
            missing_scan_form_type_message: Message when scan form type is blank.
            invalid_payment_method_message: Template for unknown payment methods.
            invalid_layout_message: Template for incompatible layouts.

        Returns:
            Validation message string when invalid, otherwise ``None``.
        """
        normalized_pm = payment_method.strip()
        normalized_sft = scan_form_type.strip()
        if not normalized_pm:
            return missing_payment_method_message
        if not normalized_sft:
            return missing_scan_form_type_message
        pm_display = PAYMENT_METHOD_LABELS.get(normalized_pm, normalized_pm)
        allowed_layouts = cls.allowed_scan_form_types_for_payment_method(normalized_pm)
        if not allowed_layouts:
            return cls._error_for_unknown_payment_method(
                normalized_pm,
                normalized_sft,
                pm_display,
                invalid_payment_method_message,
            )
        if normalized_sft not in allowed_layouts:
            return cls._error_for_incompatible_layout(
                normalized_pm, normalized_sft, pm_display, invalid_layout_message
            )
        return None

    @classmethod
    def validate_batch_request(
        cls,
        payment_method: str,
        scan_form_type: str,
        **message_overrides: str,
    ) -> None:
        """Raise ``ValueError`` when payment/layout validation fails.

        Args:
            payment_method: Batch payment method value.
            scan_form_type: Batch scan form layout value.
            **message_overrides: Optional message template overrides for
                ``batch_request_error``.

        Raises:
            ValueError: If the batch request is invalid.
        """
        error = cls.batch_request_error(
            payment_method,
            scan_form_type,
            **message_overrides,
        )
        if error is not None:
            raise ValueError(error)

    @classmethod
    def normalize_batch_request_fields(
        cls,
        *,
        r2_prefix: Any = "",
        payment_method: Any = "",
        scan_form_type: Any = "",
        batch_name: Any = "",
        auto_process: Any = True,
    ) -> dict[str, str | bool]:
        """Return normalized scan batch request fields.

        Args:
            r2_prefix: Intake prefix or folder path.
            payment_method: Batch payment method value.
            scan_form_type: Physical layout value.
            batch_name: Optional physical batch filename.
            auto_process: Bool-like value controlling OCR trigger.

        Returns:
            Dict with stripped string fields and stable boolean coercion.
        """
        return {
            "r2_prefix": str(r2_prefix or "").strip(),
            "payment_method": str(payment_method or "").strip(),
            "scan_form_type": str(scan_form_type or "").strip(),
            "batch_name": str(batch_name or "").strip(),
            "auto_process": _coerce_scan_request_bool(auto_process),
        }

    @classmethod
    def allowed_scan_form_types_for_payment_method(
        cls, payment_method: str
    ) -> tuple[str, ...]:
        """Return valid layouts for a payment method."""
        return PAYMENT_METHOD_TO_SCAN_FORM_TYPES.get(payment_method, tuple())

    @classmethod
    def allowed_scan_form_type_choices_for_payment_method(
        cls, payment_method: str
    ) -> list[tuple[str, str]]:
        """Return visible layout choices for a payment method."""
        allowed = set(cls.allowed_scan_form_types_for_payment_method(payment_method))
        return [
            (value, label)
            for value, label in SCAN_FORM_TYPE_CHOICES
            if value in allowed
        ]

    @classmethod
    def scan_form_type_pages_per_donor(cls, scan_form_type: str) -> int | None:
        """Return pages per donor for a layout value."""
        return SCAN_FORM_TYPE_PAGES.get(scan_form_type)

    @property
    def pages_per_donor(self) -> int | None:
        """Return the number of pages that make up one donor document."""
        return self.scan_form_type_pages_per_donor(self.scan_form_type)

    @property
    def progress_pct(self) -> int:
        """Return OCR processing progress percentage.

        Returns:
            int: Progress percentage clamped to 0-100.
        """
        if self.total_scans > 0:
            return min(int(self.processed_scans / self.total_scans * 100), 100)
        return 0

    @classmethod
    def final_status_for_outcomes(
        cls,
        *,
        total: int,
        matched: int,
        failed: int,
    ) -> str:
        """Return the terminal batch status for processed placeholder outcomes.

        A batch is only "completed" when every processed placeholder reached a
        fully matched terminal state. Any failed placeholder or any processed
        placeholder still requiring manual review (for example ``unmatched`` or
        ``completed`` placeholder statuses) keeps the batch at
        ``partially_completed``.

        Args:
            total: Total processed placeholders in the batch.
            matched: Placeholders that reached ``matched``.
            failed: Placeholders that reached ``failed``.

        Returns:
            Final ``ScanBatch.status`` value.
        """
        if total <= 0:
            return cls.STATUS_COMPLETED

        unresolved = max(total - matched - failed, 0)
        if failed >= total:
            return cls.STATUS_FAILED
        if failed > 0 or unresolved > 0:
            return cls.STATUS_PARTIALLY_COMPLETED
        return cls.STATUS_COMPLETED


class ScanPlaceholder(models.Model):
    """Represents a scanned donation form image in R2.

    Created by the OCR processing pipeline after scanner upload.
    Stores extracted OCR data and links to the resulting Donation once captured.

    Attributes:
        id: UUID primary key.
        batch: Foreign key to ScanBatch.
        image_url: Public URL to the scan image.
        image_path: Path in R2 bucket.
        urn: Pre-populated URN via OCR or filename.
        donor_name: Pre-populated donor name from OCR or donor lookup.
        is_captured: Whether this scan has been captured as a donation.
        donation: OneToOne link to the resulting Donation.
        ocr_data: Raw Document AI results.
        extracted_data: Structured data extracted from OCR (amount, gift_aid, etc.).
        ocr_confidence: Overall OCR confidence score (0.0-1.0).
        ocr_status: OCR processing status.
        processing_error: Error message if processing failed.
        matched_donor: Foreign key to Donor if URN matched house file.
        matched_data_file_donor: Foreign key to DataFileDonor if URN matched data file.
        matched_system_donor: Foreign key to internal donor history record.
        donor_match_candidates: Borderline fuzzy donor-match candidates for QA review.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    OCR_STATUS_PENDING = "pending"
    OCR_STATUS_PROCESSING = "processing"
    OCR_STATUS_COMPLETED = "completed"
    OCR_STATUS_FAILED = "failed"
    OCR_STATUS_MATCHED = "matched"
    OCR_STATUS_UNMATCHED = "unmatched"
    OCR_STATUS_SKIPPED = "skipped"
    OCR_STATUS_NEEDS_RESCAN = "needs_rescan"

    OCR_STATUS_CHOICES = [
        (OCR_STATUS_PENDING, "Pending"),
        (OCR_STATUS_PROCESSING, "Processing"),
        (OCR_STATUS_COMPLETED, "Completed"),
        (OCR_STATUS_FAILED, "Failed"),
        (OCR_STATUS_MATCHED, "Matched"),
        (OCR_STATUS_UNMATCHED, "Unmatched"),
        (OCR_STATUS_SKIPPED, "Skipped (no Document AI)"),
        (OCR_STATUS_NEEDS_RESCAN, "Needs rescan"),
    ]

    REDACTION_PENDING = "pending"
    REDACTION_IN_PROGRESS = "in_progress"
    REDACTION_CVV_PENDING = "cvv_pending"
    REDACTION_DEFERRED = "deferred"
    REDACTION_COMPLETED = "completed"
    REDACTION_BLOCKED = "blocked"

    REDACTION_STATUS_CHOICES = [
        (REDACTION_PENDING, "Pending redaction"),
        (REDACTION_IN_PROGRESS, "Redaction in progress"),
        (REDACTION_CVV_PENDING, "Coordinates saved, CVV pass not yet applied"),
        (REDACTION_DEFERRED, "CVV applied, post-charge pass pending"),
        (REDACTION_COMPLETED, "Redaction completed"),
        (REDACTION_BLOCKED, "Blocked"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        ScanBatch, on_delete=models.CASCADE, related_name="placeholders"
    )
    image_url = models.URLField(max_length=1000)
    image_path = models.CharField(max_length=500, help_text="Path in R2 bucket")

    # Pre-populated via OCR or Filename
    urn = models.CharField(max_length=50, blank=True, db_index=True)
    donor_name = models.CharField(max_length=255, blank=True)

    # Status tracking
    is_captured = models.BooleanField(default=False, db_index=True)
    donation = models.OneToOneField(
        "donations.Donation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_placeholder",
    )

    # OCR results
    ocr_data = models.JSONField(
        default=dict, blank=True, help_text="Raw Document AI results"
    )
    extracted_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Structured data extracted from OCR: "
            '{"amount": "25.00", "gift_aid": true, "payment_method": "cheque", '
            '"donation_date": "15/01/2026", "appeal_code": "...", ...}'
        ),
    )
    ocr_confidence = models.FloatField(
        default=0.0,
        help_text="Overall OCR confidence score (0.0-1.0)",
    )
    ocr_status = models.CharField(
        max_length=20,
        choices=OCR_STATUS_CHOICES,
        default=OCR_STATUS_PENDING,
        db_index=True,
        help_text="OCR processing status",
    )

    processing_error = models.TextField(blank=True)

    # Multi-page document support — stores all R2 keys that make up this donor's document.
    # Empty list = single-page (backward-compatible). Populated when the batch's
    # scan_form_type requires more than one page per donor (duplex, *_with_payment).
    page_keys = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "All R2 keys for this donor's document pages. "
            "Empty = single page; populated only for multi-page form types."
        ),
    )
    original_page_keys = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Snapshot of R2 keys at ingest (before manual redaction). "
            "Used for redaction workflow and audit."
        ),
    )
    redaction_status = models.CharField(
        max_length=20,
        choices=REDACTION_STATUS_CHOICES,
        default=REDACTION_COMPLETED,
        db_index=True,
        help_text=(
            "Manual redaction state. RedactionSettings determines which "
            "payment methods require redaction before QA approval."
        ),
    )
    redaction_notes = models.TextField(
        blank=True,
        default="",
        help_text="Optional notes from the staff member who completed redaction",
    )
    redaction_error = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Last redaction failure reason. Populated when redaction_status is "
            "'blocked' so QA can see why the upload failed without losing the "
            "previous (unredacted) state."
        ),
    )
    redaction_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When manual redaction was marked complete",
    )
    cvv_redacted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the CVV-only redaction pass was applied to the R2 image. "
            "PCI DSS Requirement 3.2 requires CVV/CVC to be redacted on "
            "the auth attempt, not after a successful charge — so this "
            "fires before redaction_completed_at."
        ),
    )
    redaction_coords_cvv = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Per-page CVV-only blackout rectangles saved by QA. Applied to "
            "the R2 image by apply_cvv_redaction_task on every Stripe "
            "PaymentIntent.create() return (success or failure). Shape: "
            'list of pages, each page a list of {"x", "y", "width", '
            '"height"} rect dicts in 0..1 normalised coords.'
        ),
    )
    redaction_coords_post_charge = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Per-page post-charge blackout rectangles (PAN, expiry, "
            "signature, etc.) saved by QA. Applied to the R2 image by "
            "apply_deferred_redaction_task once the charge succeeds, the "
            "operator rejects the donation, or the retention TTL expires. "
            "Shape matches redaction_coords_cvv."
        ),
    )
    redacted_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redacted_scan_placeholders",
        help_text="Staff user who uploaded redacted pages",
    )
    donor_pdf_key = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        help_text=(
            "R2 key for the assembled per-donor PDF (all pages merged into one file). "
            "Used by the client portal to serve a viewable copy of the donor's form."
        ),
    )

    # QR code identification (warm records only)
    qr_decoded = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "True if a QR code was successfully decoded from the scan image. "
            "Warm records always have qr_decoded=True."
        ),
    )
    qr_raw = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Raw QR code string decoded from the scan (appeal_code|package_code|urn).",
    )

    # Donor matching results
    matched_donor = models.ForeignKey(
        "donors.Donor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_placeholders",
        help_text="House file donor matched via URN lookup",
    )
    matched_data_file_donor = models.ForeignKey(
        "donors.DataFileDonor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_placeholders",
        help_text="Data file donor matched via URN lookup",
    )
    matched_system_donor = models.ForeignKey(
        "donors.SystemDonor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_placeholders",
        help_text="Internal donor profile created or resolved for this scan",
    )

    # Fuzzy donor-match candidates surfaced to QA when the matcher finds a
    # borderline name+postcode hit (Jaro-Winkler similarity in [0.85, 0.95)).
    # Stored as a list of dicts ``{"urn": str, "score": float, "name": str,
    # "system_donor_id": str}`` so the QA reviewer can pick the right donor
    # without a follow-up DB lookup. Populated by
    # :func:`scans.scan_processing_donors._record_fuzzy_candidates`; empty list
    # means either an exact/identifier auto-match won or no plausible match
    # was found.
    donor_match_candidates = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Borderline donor-match candidates (Jaro-Winkler 0.85-0.95) "
            "surfaced for QA reviewer disambiguation. Each entry holds "
            "{urn, score, name, system_donor_id}."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scan Placeholder"
        verbose_name_plural = "Scan Placeholders"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["batch", "ocr_status"], name="scan_batch_status_idx"),
            models.Index(fields=["urn", "is_captured"], name="scan_urn_captured_idx"),
            models.Index(
                fields=["redaction_status", "-updated_at"],
                name="scan_redaction_stat_upd_idx",
            ),
        ]
        permissions = [
            (
                "view_unredacted_scan",
                "Can view scan images before manual redaction is complete",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the scan placeholder.

        Returns:
            str: URN and batch name.
        """
        return f"Scan {self.urn or 'Unknown'} in {self.batch.batch_name}"

    @property
    def is_matched(self) -> bool:
        """Return True if a donor match was found.

        Returns:
            bool: Whether a donor was matched via URN.
        """
        return (
            self.matched_donor is not None or self.matched_data_file_donor is not None
        )

    @property
    def extracted_amount(self) -> str:
        """Return extracted donation amount from OCR data.

        Returns:
            str: The amount string or empty string.
        """
        return str(self.extracted_data.get("amount", ""))

    @property
    def extracted_gift_aid(self) -> bool | None:
        """Return extracted Gift Aid flag from OCR data.

        Returns:
            bool | None: True/False if detected, None if not found.
        """
        return self.extracted_data.get("gift_aid")


PAYMENT_METHOD_REDACTION_FIELD_MAP: dict[str, str] = {
    "card": "require_for_card",
    "direct_debit": "require_for_direct_debit",
    "cash": "require_for_cash",
    "caf": "require_for_caf",
    "cheque": "require_for_cheque",
    "postal_order": "require_for_postal_order",
    "non_financial": "require_for_non_financial",
}


class RedactionSettings(models.Model):
    """Per-payment-method gate for QA manual redaction.

    Singleton: exactly one row should exist (pk=1). Determines which payment
    methods require the QA reviewer to apply manual redaction to the scanned
    form before the donation can be approved. Methods not listed here can be
    approved without manual redaction.
    """

    require_for_card = models.BooleanField(
        default=True,
        help_text="QA must redact the scan before approving Card donations.",
    )
    require_for_direct_debit = models.BooleanField(
        default=True,
        help_text="QA must redact the scan before approving Direct Debit donations.",
    )
    require_for_cash = models.BooleanField(
        default=False,
        help_text="QA must redact the scan before approving Cash donations.",
    )
    require_for_caf = models.BooleanField(
        default=False,
        help_text="QA must redact the scan before approving CAF Voucher donations.",
    )
    require_for_cheque = models.BooleanField(
        default=False,
        help_text="QA must redact the scan before approving Cheque donations.",
    )
    require_for_postal_order = models.BooleanField(
        default=False,
        help_text="QA must redact the scan before approving Postal Order donations.",
    )
    require_for_non_financial = models.BooleanField(
        default=False,
        help_text="QA must redact the scan before approving Non-Financial donations.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redaction_settings_updates",
    )

    class Meta:
        verbose_name = "Redaction Settings"
        verbose_name_plural = "Redaction Settings"

    def __str__(self) -> str:
        """Return string representation."""
        return "Redaction Settings"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Force pk=1 so only one row can exist."""
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls) -> RedactionSettings:
        """Return the singleton row, creating it with defaults if missing."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def requires_redaction(self, payment_method: str) -> bool:
        """Return True when *payment_method* requires redaction before approval."""
        field = PAYMENT_METHOD_REDACTION_FIELD_MAP.get(payment_method or "")
        if field is None:
            return False
        return bool(getattr(self, field))

    def required_payment_methods(self) -> list[str]:
        """Return the payment-method strings currently flagged as required."""
        return [
            method
            for method, field in PAYMENT_METHOD_REDACTION_FIELD_MAP.items()
            if bool(getattr(self, field))
        ]


class ScanUploadProgress(models.Model):
    """Track scanner upload progress per campaign.

    Updated via the scan-upload webhook every time the scanner workstation
    pushes new images. Polled by the batch entry UI for live progress.

    Attributes:
        campaign: OneToOne primary key to Campaign.
        total_uploaded: Number of scans uploaded so far.
        total_expected: Expected number of scans (0 = unknown).
        latest_urn: URN of the most recently uploaded scan.
        status: Upload status (idle/scanning/complete/error).
        last_upload_at: Timestamp of last upload.
        last_error: Most recent error message reported by the scanner.
        updated_at: Timestamp when last updated (auto-set on every webhook).
        last_completion_request_hash: SHA-256 of the canonical complete-status
            payload, used to deduplicate replayed webhook deliveries.
        last_completion_task_id: Celery task id dispatched on the most recent
            complete-status webhook.
        last_completion_dispatched_at: Timestamp the last complete-status
            webhook dispatched a Celery task.
    """

    STATUS_IDLE = "idle"
    STATUS_SCANNING = "scanning"
    STATUS_COMPLETE = "complete"
    STATUS_ERROR = "error"

    STATUS_CHOICES = [
        (STATUS_IDLE, "Idle"),
        (STATUS_SCANNING, "Scanning"),
        (STATUS_COMPLETE, "Complete"),
        (STATUS_ERROR, "Error"),
    ]

    campaign = models.OneToOneField(
        "campaigns.Campaign",
        on_delete=models.CASCADE,
        related_name="scan_progress",
        primary_key=True,
    )
    total_uploaded = models.PositiveIntegerField(default=0)
    total_expected = models.PositiveIntegerField(
        default=0,
        help_text="Expected number of scans (0 = unknown).",
    )
    latest_urn = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="idle")
    last_upload_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="Most recent error message reported by the scanner workstation.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    last_completion_request_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "SHA-256 of the most recent complete-status webhook payload. "
            "Used to short-circuit replayed deliveries."
        ),
    )
    last_completion_task_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Celery task id dispatched for the last complete-status webhook.",
    )
    last_completion_dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Scan Upload Progress"
        verbose_name_plural = "Scan Upload Progress"
        indexes = [
            # Hot path: ``scans/scan_folder.py:watch_r2_scan_folders_task`` and
            # the stale-progress sweep in ``scans/tasks.py:cleanup_stale_scan_progress_task``
            # filter by (campaign, status). With 5+ concurrent scanners and
            # one row per active campaign, this composite index keeps the
            # webhook hot path on a single index lookup instead of a scan.
            models.Index(
                fields=["campaign", "status"],
                name="scanprogress_camp_stat_idx",
            ),
            # Donor lookup support: the scan-upload webhook in
            # ``core/webhooks.py`` writes ``latest_urn`` per campaign and
            # downstream reconciliation surfaces query by URN. Indexing
            # keeps point lookups cheap when the table grows to one row
            # per campaign across many concurrent scanners.
            models.Index(
                fields=["latest_urn"],
                name="scanprogress_latest_urn_idx",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of scan upload progress.

        Returns:
            str: Campaign and upload count.
        """
        return f"ScanProgress({self.campaign}) — {self.total_uploaded} uploaded"

    @property
    def progress_pct(self) -> int:
        """Return completion percentage (0-100).

        Returns:
            int: Progress percentage clamped to 0-100.
        """
        if self.total_expected and self.total_expected > 0:
            return min(int(self.total_uploaded / self.total_expected * 100), 100)
        return 0
