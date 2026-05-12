"""Donation model for the donation management system."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import models
from django.utils import timezone
from encrypted_fields import fields as encrypted_fields

if TYPE_CHECKING:
    pass


# Reviewer claim window: how long a single user holds a batch before the
# claim is considered stale. Surfaced for tests and the periodic cleanup task.
REVIEWER_LOCK_TTL = timedelta(minutes=30)


class Donation(models.Model):
    """Actual donation entry (filled form submission).

    Represents a single donation with full details including donor source,
    payment method, financial information, QA status, and letter tracking.

    Attributes:
        id: UUID primary key.
        campaign: Foreign key to Campaign model.
        batch: Foreign key to DonationBatch model.
        donor_source: Source of donor (house_file or data_file).
        donor: Optional foreign key to imported house-file donor source row.
        data_file_donor: Optional foreign key to imported campaign data-file row.
        system_donor: Permanent internal donor profile for system history.
        paying_in_slip: Foreign key to PayingInSlip for banking.
        amount: Donation amount.
        currency: Currency code.
        payment_method: Payment method used.
        donation_date: Date of donation.
        gift_aid: Gift Aid declaration flag.
        sort_code: UK bank sort code (direct debit only, encrypted at rest).
        account_number: UK bank account number (direct debit only, encrypted at rest).
        payment_status: Online payment processing status.
        qa_status: QA review status.
        qa_notes: Reviewer notes.
        letter_status: Thank-you letter status.
        letter_batch: Foreign key to LetterBatch.
        donation_frequency: Donation frequency.
        field_data: Custom campaign field values.
        package_codes: Many-to-many to PackageCode.
        filled_by: User who entered the donation.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    CURRENCY_CHOICES = [
        ("GBP", "£ GBP"),
        ("USD", "$ USD"),
        ("EUR", "€ EUR"),
    ]

    PAYMENT_METHOD_CARD = "card"
    PAYMENT_METHOD_DIRECT_DEBIT = "direct_debit"
    PAYMENT_METHOD_CASH = "cash"
    PAYMENT_METHOD_CAF = "caf"
    PAYMENT_METHOD_CHEQUE = "cheque"
    PAYMENT_METHOD_POSTAL_ORDER = "postal_order"
    PAYMENT_METHOD_NON_FINANCIAL = "non_financial"

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_METHOD_CARD, "Card"),
        (PAYMENT_METHOD_DIRECT_DEBIT, "Direct Debit"),
        (PAYMENT_METHOD_CASH, "Cash"),
        (PAYMENT_METHOD_CAF, "CAF Voucher"),
        (PAYMENT_METHOD_CHEQUE, "Cheque"),
        (PAYMENT_METHOD_POSTAL_ORDER, "Postal Order"),
        (PAYMENT_METHOD_NON_FINANCIAL, "Non Financial/No Payment"),
    ]

    PAYMENT_STATUS_PENDING = "pending"
    PAYMENT_STATUS_PROCESSING = "processing"
    PAYMENT_STATUS_COMPLETED = "completed"
    PAYMENT_STATUS_FAILED = "failed"
    # MOTO (phone) intake authorised the card during the call but settlement
    # is deferred to QA approval. ``capture_method="manual"`` on the Stripe
    # PaymentIntent means the donor sees a pending hold on their card and
    # the funds move only when QA approves; on QA reject we cancel the
    # intent (no refund cycle). Stripe authorisations expire after 7 days,
    # so a Celery beat task warns QA when an intent approaches expiry.
    PAYMENT_STATUS_REQUIRES_CAPTURE = "requires_capture"
    # 3DS / SCA challenge required — donor must complete authentication via a
    # Stripe-hosted Checkout link. Distinct from "failed" so QA can dispatch
    # an authentication email instead of marking the donation lost.
    PAYMENT_STATUS_AWAITING_AUTHENTICATION = "awaiting_authentication"
    PAYMENT_STATUS_REFUNDED = "refunded"
    PAYMENT_STATUS_DISPUTED = "disputed"
    PAYMENT_STATUS_DISPUTE_LOST = "dispute_lost"
    # Distinct from FAILED so reports can separate genuine cheque/cash bounces
    # (operator marked the slip as ``issues`` / ``partial_success`` / ``failed``)
    # from Stripe-side card failures. See ``BankingService.reverse_donations_for_slip``.
    #
    # Stripe/DB divergence contract: when ``payment_status == "reversed"`` and
    # ``payment_method == "card"``, the source of truth for what Stripe holds
    # is ``StripePayment.status`` on the related ``stripe_payments`` set, NOT
    # this field. The ``reversed`` flag is a DB-side signal that banking
    # flagged a card donation as part of a failed slip; until an operator
    # issues a Stripe refund (which lands via the ``charge.refunded`` webhook
    # and flips this field to ``refunded``), Stripe still holds the customer's
    # funds. Reports should join on ``StripePayment.status`` for true cash
    # position rather than reading this field for card donations.
    PAYMENT_STATUS_REVERSED = "reversed"

    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_STATUS_PENDING, "Pending"),
        (PAYMENT_STATUS_PROCESSING, "Processing"),
        (PAYMENT_STATUS_COMPLETED, "Completed"),
        (PAYMENT_STATUS_FAILED, "Failed"),
        (PAYMENT_STATUS_REQUIRES_CAPTURE, "Requires Capture (MOTO auth)"),
        (PAYMENT_STATUS_AWAITING_AUTHENTICATION, "Awaiting Authentication"),
        (PAYMENT_STATUS_REFUNDED, "Refunded"),
        (PAYMENT_STATUS_DISPUTED, "Disputed"),
        (PAYMENT_STATUS_DISPUTE_LOST, "Dispute Lost"),
        (PAYMENT_STATUS_REVERSED, "Reversed (Banking)"),
    ]

    FREQUENCY_CHOICES = [
        ("one_time", "One Time"),
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("annually", "Annually"),
    ]

    DONOR_SOURCE_CHOICES = [
        ("house_file", "House File"),
        ("data_file", "Data File"),
    ]

    QA_STATUS_PENDING = "pending"
    QA_STATUS_APPROVED = "approved"
    QA_STATUS_REJECTED = "rejected"
    QA_STATUS_FLAGGED = "flagged"

    QA_STATUS_CHOICES = [
        (QA_STATUS_PENDING, "Pending"),
        (QA_STATUS_APPROVED, "Approved"),
        (QA_STATUS_REJECTED, "Rejected"),
        (QA_STATUS_FLAGGED, "Flagged"),
    ]

    # Standardised reject reason picklist. The selected label is surfaced to
    # donors via failure_template letter context, so free text is avoided
    # except when 'other' is chosen (which requires qa_notes detail).
    QA_REJECT_REASON_ILLEGIBLE = "illegible"
    QA_REJECT_REASON_NO_AMOUNT = "no_amount"
    QA_REJECT_REASON_NO_PAYMENT = "no_payment"
    QA_REJECT_REASON_AMOUNT_MISMATCH = "amount_mismatch"
    QA_REJECT_REASON_MISSING_SIGNATURE = "missing_signature"
    QA_REJECT_REASON_DAMAGED_FORM = "damaged_form"
    QA_REJECT_REASON_DUPLICATE = "duplicate"
    QA_REJECT_REASON_CHEQUE_ISSUE = "cheque_issue"
    QA_REJECT_REASON_MISSING_DONOR_DETAILS = "missing_donor_details"
    QA_REJECT_REASON_OTHER = "other"

    QA_REJECT_REASON_CHOICES = [
        (QA_REJECT_REASON_ILLEGIBLE, "Illegible / cannot read form"),
        (QA_REJECT_REASON_NO_AMOUNT, "Missing donation amount"),
        (QA_REJECT_REASON_NO_PAYMENT, "No payment enclosed"),
        (QA_REJECT_REASON_AMOUNT_MISMATCH, "Amount mismatch (form vs payment)"),
        (QA_REJECT_REASON_MISSING_SIGNATURE, "Missing signature (Gift Aid)"),
        (QA_REJECT_REASON_DAMAGED_FORM, "Damaged or incomplete form"),
        (QA_REJECT_REASON_DUPLICATE, "Duplicate submission"),
        (
            QA_REJECT_REASON_CHEQUE_ISSUE,
            "Cheque issue (unsigned / post-dated / wrong payee)",
        ),
        (QA_REJECT_REASON_MISSING_DONOR_DETAILS, "Missing donor details"),
        (QA_REJECT_REASON_OTHER, "Other (see notes)"),
    ]

    # Field names skipped by the audit pre/post-save signal handlers in
    # ``audit/signals.py``. These columns are encrypted at rest via
    # ``django-fernet-encrypted-fields``; the signal sees the decrypted
    # plaintext, so storing it in ``AuditLog.changes`` would leak the very
    # PII the encryption is meant to protect. Excluding the field names
    # also keeps them out of audit metadata entirely.
    audit_exclude_fields = ("sort_code", "account_number")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT (audit 2026-05-02 §4.1): a campaign with donations cannot be
    # hard-deleted — operators should ``Campaign.archive()`` instead. Cascade
    # would have wiped financial-record rows in one click.
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, related_name="donations"
    )

    # Reference to Batch (REQUIRED - all donations must be part of a batch)
    batch = models.ForeignKey(
        "donations.DonationBatch",
        on_delete=models.CASCADE,
        related_name="donations",
        help_text="Batch this donation belongs to (required)",
    )

    # Donor Source Selection
    donor_source = models.CharField(
        max_length=20,
        choices=DONOR_SOURCE_CHOICES,
        default="house_file",
        help_text="Source of donor: House File (main) or Data File (campaign-specific)",
    )

    # Reference to imported House File donor source row
    donor = models.ForeignKey(
        "donors.Donor",
        on_delete=models.SET_NULL,
        related_name="donations",
        null=True,
        blank=True,
        help_text="Matched imported house-file donor source row",
    )

    # Reference to imported Data File donor source row
    data_file_donor = models.ForeignKey(
        "donors.DataFileDonor",
        on_delete=models.SET_NULL,
        related_name="donations",
        null=True,
        blank=True,
        help_text="Matched imported campaign data-file donor source row",
    )
    system_donor = models.ForeignKey(
        "donors.SystemDonor",
        on_delete=models.PROTECT,
        related_name="donations",
        null=True,
        blank=True,
        help_text="Permanent internal donor profile for donation history",
    )

    # Reference to Paying-In Slip (for banking reconciliation)
    paying_in_slip = models.ForeignKey(
        "banking.PayingInSlip",
        on_delete=models.SET_NULL,
        related_name="donations",
        null=True,
        blank=True,
        help_text="Paying-in slip for bank statement reconciliation",
    )

    # Donation Details
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="Donation amount"
    )
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default="GBP",
        help_text="Currency of donation",
    )
    payment_method = models.CharField(
        max_length=30,
        choices=PAYMENT_METHOD_CHOICES,
        default="card",
        help_text="Payment method used",
    )
    donation_date = models.DateField(
        null=True, blank=True, help_text="Date of donation"
    )

    # Payment Method Specific Fields

    # Card payment fields (for card and direct_debit)
    card_holder_name = models.CharField(
        max_length=255, blank=True, help_text="Card holder name (for card/direct debit)"
    )
    card_last_four = models.CharField(
        max_length=4,
        blank=True,
        help_text="Last 4 digits of card number (PCI-DSS compliant — never store full card number)",
    )
    card_expiry_date = models.CharField(
        max_length=7, blank=True, help_text="Card expiry date (MM/YYYY format)"
    )

    # Direct Debit specific fields
    direct_debit_start_date = models.DateField(
        null=True, blank=True, help_text="Direct debit start date"
    )
    direct_debit_end_date = models.DateField(
        null=True, blank=True, help_text="Direct debit end date"
    )
    # Bank account details for direct debit donations. ``null=True`` is
    # required because EncryptedCharField.get_prep_value("") returns ``None``
    # — the encryption layer writes NULL for empty strings.
    sort_code = encrypted_fields.EncryptedCharField(
        max_length=6,
        blank=True,
        null=True,
        default="",
        help_text="UK bank sort code for direct debits (6 digits, encrypted at rest)",
    )
    account_number = encrypted_fields.EncryptedCharField(
        max_length=8,
        blank=True,
        null=True,
        default="",
        help_text="UK bank account number for direct debits (8 digits, encrypted at rest)",
    )

    # Cheque fields (existing)
    cheque_number = models.CharField(
        max_length=50, blank=True, help_text="Cheque number (for cheque payments)"
    )
    cheque_date = models.DateField(
        null=True, blank=True, help_text="Cheque date (for cheque payments)"
    )

    # CAF (Charities Aid Foundation) fields
    caf_voucher_number = models.CharField(
        max_length=100, blank=True, help_text="CAF voucher/cheque number"
    )
    caf_donor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="CAF donor name (may differ from main donor)",
    )
    caf_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="CAF voucher amount",
    )

    # Postal Order fields
    postal_order_number = models.CharField(
        max_length=50, blank=True, help_text="Postal order/cheque number"
    )
    postal_order_date = models.DateField(
        null=True, blank=True, help_text="Postal order date"
    )
    postal_issuer = models.CharField(
        max_length=255, blank=True, help_text="Post office or issuer name"
    )

    # Non-Financial/No Payment fields
    non_financial_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason for non-financial donation (e.g., In-Kind, Volunteering)",
    )
    non_financial_notes = models.TextField(
        blank=True, help_text="Additional notes for non-financial donations"
    )

    # Gift Aid (UK Tax Relief)
    gift_aid = models.BooleanField(
        default=False, help_text="Gift Aid declaration for eligible UK taxpayers"
    )

    # Stripe Payment Integration (for online card donations)
    payment_status = models.CharField(
        max_length=32,
        choices=PAYMENT_STATUS_CHOICES,
        default=PAYMENT_STATUS_PENDING,
        db_index=True,
        help_text="Online payment processing status",
    )
    # Free-text explanation captured when ``BankingService.reverse_donations_for_slip``
    # flips ``payment_status`` to ``reversed`` (cheque bounce, cash count short, etc.).
    # Blank for any other lifecycle path. Surfaced in audit log diffs and the QA UI.
    payment_reversal_reason = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Reason recorded when banking reversed this donation's payment "
            "(cheque bounce, cash discrepancy). Blank otherwise."
        ),
    )

    # QA review status for one-on-one donation verification
    qa_status = models.CharField(
        max_length=20,
        choices=QA_STATUS_CHOICES,
        default=QA_STATUS_PENDING,
        help_text="QA review status for this donation",
        db_index=True,
    )
    qa_notes = models.TextField(
        blank=True,
        default="",
        help_text="Reviewer notes for donation-level QA actions",
    )
    qa_reject_reason = models.CharField(
        max_length=32,
        choices=QA_REJECT_REASON_CHOICES,
        blank=True,
        default="",
        help_text=(
            "Standardised reject reason shown to donors in failure letters. "
            "Required when qa_status == 'rejected'; blank otherwise."
        ),
    )
    # Fuzzy donor-match candidates surfaced to QA when the matcher finds a
    # borderline name+postcode hit (Jaro-Winkler similarity in
    # [0.85, 0.95)). Mirror of
    # :attr:`scans.models.ScanPlaceholder.donor_match_candidates`: each entry
    # is a dict ``{"urn", "score", "name", "system_donor_id"}`` so the QA
    # UI can render a candidate ranking without a follow-up DB lookup.
    # Populated by
    # :func:`scans.scan_processing_donations.create_donation_from_placeholder`;
    # empty list means no fuzzy candidates were found (either an
    # exact/identifier auto-match, or no plausible match at all). Pre-
    # migration placeholders may instead carry a list of stringified
    # ``SystemDonor`` PKs (legacy bare-PK shape) — readers must accept both
    # shapes during the transition window documented on
    # :func:`scans.scan_processing_donors._record_fuzzy_candidates`.
    donor_match_candidates = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Borderline donor-match candidates (Jaro-Winkler 0.85-0.95) "
            "surfaced for QA reviewer disambiguation. Each entry holds "
            "{urn, score, name, system_donor_id}; legacy rows may store a "
            "bare PK string list — readers must accept both shapes."
        ),
    )

    # Letter tracking fields
    LETTER_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("generated", "Generated"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    ]
    letter_status = models.CharField(
        max_length=20,
        choices=LETTER_STATUS_CHOICES,
        default="pending",
        help_text="Status of thank-you letter for this donation",
    )
    letter_generated_at = models.DateTimeField(
        null=True, blank=True, help_text="When the thank-you letter was generated"
    )
    letter_batch = models.ForeignKey(
        "letters.LetterBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="donations",
        help_text="Letter batch this donation belongs to",
    )
    # Set when an already-generated/sent thank-you letter is invalidated by a
    # downstream event (banking reversal, refund). The original ``letter_status``
    # is preserved so history is not lost — readers should treat
    # ``letter_voided_at IS NOT NULL`` as the authoritative "this letter is no
    # longer valid" signal regardless of ``letter_status``.
    letter_voided_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when an already-generated/sent thank-you letter is invalidated "
            "by a downstream event (banking reversal, refund). The original "
            "letter_status is preserved so history is not lost."
        ),
    )
    letter_void_reason = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Why the issued letter was voided (mirrors payment_reversal_reason "
            "on banking-driven voids)."
        ),
    )

    # Donation Frequency
    donation_frequency = models.CharField(
        max_length=20,
        choices=FREQUENCY_CHOICES,
        blank=True,
        help_text="Donation frequency (optional)",
    )

    # Custom field data (from campaign fields)
    field_data = models.JSONField(
        default=dict, blank=True, help_text="Custom campaign field values"
    )

    # Per-donation list of OCR fields whose confidence fell below the campaign
    # threshold. When non-empty, the donation requires explicit reviewer action
    # and is excluded from the batch-approval auto-approve cascade. Each entry
    # is a dict ``{"field": <name>, "confidence": <float>}`` so the UI can
    # surface both the field and the score it scraped.
    low_confidence_fields = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "OCR fields whose confidence fell below the campaign threshold. "
            "When populated, the donation is excluded from auto-approval and "
            "requires explicit reviewer sign-off."
        ),
    )

    # Package codes for this donation
    package_codes = models.ManyToManyField(
        "campaigns.PackageCode",
        related_name="donations",
        blank=True,
        help_text="Package codes associated with this donation",
    )

    # Metadata
    filled_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="filled_donations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("verify_donations", "Can verify donations"),
        ]
        indexes = [
            models.Index(
                fields=["campaign", "created_at"], name="donation_camp_created_idx"
            ),
            models.Index(
                fields=["campaign", "donation_date"], name="donation_camp_date_idx"
            ),
            models.Index(fields=["payment_method"], name="donation_payment_idx"),
            models.Index(fields=["donor"], name="donation_donor_idx"),
            models.Index(fields=["data_file_donor"], name="donation_datadonor_idx"),
            models.Index(fields=["system_donor"], name="donation_system_donor_idx"),
            models.Index(fields=["qa_status"], name="donation_qa_status_idx"),
            # Audit 2026-05-02 §4.2: QA dashboard filters by qa_status / payment_status
            # and orders by ``-created_at``. Composite indexes cover the WHERE +
            # ORDER BY in one index seek; Postgres uses the leftmost column to
            # satisfy single-column qa_status / payment_status lookups too.
            models.Index(
                fields=["qa_status", "-created_at"],
                name="donation_qa_status_ts_idx",
            ),
            models.Index(
                fields=["payment_status", "-created_at"],
                name="donation_pay_status_ts_idx",
            ),
            models.Index(
                fields=["paying_in_slip", "qa_status", "payment_method"],
                name="donation_banking_idx",
            ),
            models.Index(
                fields=["batch", "paying_in_slip"], name="donation_batch_slip_idx"
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the donation.

        Returns:
            str: Donation ID, campaign name, and date.
        """
        return f"Donation #{self.id} - {self.campaign.name} ({self.created_at.date()})"


class DonationBatch(models.Model):
    """Donation batch for group entry tracking.

    Groups related donations entered together in a single session.
    Tracks total donations and amount for the batch.

    Attributes:
        id: Auto-increment primary key.
        campaign: Foreign key to Campaign model.
        batch_name: Name or identifier for this batch.
        status: QA workflow status. Batches enter at pending_qa via OCR processing.
        default_payment_method: Default payment method for batch donations.
        default_currency: Default currency for donations in this batch.
        created_by: User who created this batch.
        total_donations: Count of donations in this batch.
        total_amount: Total monetary amount of all donations in batch.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        reviewed_by: User who reviewed/approved the batch.
        reviewed_at: Timestamp when batch was reviewed.
        review_notes: Notes from QA review.
        donations: Reverse relation to Donation model.
    """

    CURRENCY_CHOICES = [
        ("GBP", "£ GBP"),
        ("USD", "$ USD"),
        ("EUR", "€ EUR"),
    ]

    # QA Workflow Status Choices — batches enter the pipeline at pending_qa (created by OCR).
    STATUS_PENDING_QA = "pending_qa"
    STATUS_IN_REVIEW = "in_review"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING_QA, "Pending QA"),
        (STATUS_IN_REVIEW, "In Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.AutoField(primary_key=True)
    # PROTECT (audit 2026-05-02 §4.1): see Donation.campaign comment.
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, related_name="donation_batches"
    )
    batch_name = models.CharField(
        max_length=255, help_text="Name/identifier for this batch"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING_QA,
        help_text="QA workflow status for this batch",
        db_index=True,
    )
    default_payment_method = models.CharField(
        max_length=50,
        default="card",
        help_text="Default payment method for donations in this batch",
    )
    default_currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default="GBP",
        help_text="Default currency for donations in this batch (immutable after creation)",
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_batches",
    )
    reviewed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_batches",
        help_text="User who reviewed/approved the batch",
    )
    reviewed_at = models.DateTimeField(
        null=True, blank=True, help_text="When batch was reviewed"
    )
    review_notes = models.TextField(
        blank=True, default="", help_text="Notes from QA review"
    )
    total_donations = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Total amount in this batch",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Payment Processing Fields
    payment_status = models.CharField(
        max_length=30,
        choices=[
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("partially_completed", "Partially Completed"),
        ],
        default="pending",
        db_index=True,
        help_text="Payment processing status for this batch",
    )
    payment_initiated_at = models.DateTimeField(
        null=True, blank=True, help_text="When payment processing started"
    )
    payment_completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When payment processing completed"
    )
    payment_initiated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batch_payments_initiated",
        help_text="User who initiated payment processing",
    )
    successful_payment_count = models.IntegerField(
        default=0, help_text="Count of successful credit card payments"
    )
    failed_payment_count = models.IntegerField(
        default=0, help_text="Count of failed credit card payments"
    )

    # Flexible metadata — used by automated tasks to store generated artefact paths
    # e.g. {"gift_aid_report_path": "gift_aid_reports/batch_7_....csv"}
    field_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Flexible metadata and generated artefact paths for this batch",
    )

    # Dedicated path for the HMRC Gift Aid CSV generated on batch approval.
    # Stored as a MEDIA_ROOT-relative path, e.g. "gift_aid_reports/gift_aid_batch_7_20260321.csv"
    gift_aid_report_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Media-relative path to the generated HMRC Gift Aid CSV for this batch",
    )

    # Set by an operator when the Gift Aid CSV has actually been handed to HMRC.
    # ``gift_aid_report_path`` alone is auto-populated for every gift-aid batch
    # at QA approval, so it cannot distinguish "CSV generated" from "CSV
    # submitted". Banking-reversal cascade gates its HMRC-retraction warning on
    # this timestamp specifically so the alert only fires when retraction is
    # actually relevant.
    gift_aid_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Operator marks this when the Gift Aid CSV has been handed to HMRC. "
            "Used to gate the 'retraction needed' notification on banking "
            "reversal — without this, every gift-aid reversal would warn even "
            "when the CSV was never actually submitted, training operators to "
            "ignore the alert."
        ),
    )

    # Reviewer claim ("soft lock"). Two QA reviewers can otherwise open the
    # same batch concurrently and race on saves. ``reviewer_locked_at`` ages
    # the claim out after :data:`REVIEWER_LOCK_TTL` so an abandoned tab does
    # not block the queue forever.
    reviewer_locked_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_batches",
        help_text="QA reviewer who currently holds this batch.",
    )
    reviewer_locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the current reviewer claim was acquired or refreshed.",
    )

    if TYPE_CHECKING:
        from django.db.models.manager import RelatedManager

        donations: RelatedManager[Donation]

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Donation Batch"
        verbose_name_plural = "Donation Batches"
        indexes = [
            # Hot path: ``custom_admin/views/qa_review.py:qa_dashboard`` filters
            # batches by (campaign, status) and orders by ``-created_at`` for
            # the QA review listing. Composite index covers the WHERE + ORDER
            # BY in a single seek + range scan, expected to drop the QA
            # dashboard query from a sequential scan/sort to a single index
            # scan as the QA review queue scales to 100 reviewers. Postgres
            # uses the leftmost prefix of this 3-column index to satisfy
            # plain ``(campaign, status)`` lookups, so a separate 2-column
            # index would be redundant.
            models.Index(
                fields=["campaign", "status", "created_at"],
                name="batch_camp_stat_created_idx",
            ),
            # Hot path: payment retry / monitor queries (e.g. campaign
            # payment status updates in ``payments/campaign_payment.py``)
            # filter batches by (campaign, payment_status). Composite index
            # avoids a sequential scan when batch counts grow per campaign.
            models.Index(
                fields=["campaign", "payment_status"],
                name="batch_camp_paystat_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "batch_name"],
                name="donationbatch_campaign_batch_name_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the donation batch.

        Returns:
            str: Batch name and total donations count.
        """
        return f"{self.batch_name} - {self.total_donations} donations"

    def reviewer_lock_is_active(self, *, now: datetime | None = None) -> bool:
        """Return whether a non-stale reviewer lock currently exists.

        A lock counts as active when both ``reviewer_locked_by`` and
        ``reviewer_locked_at`` are set and the timestamp is newer than
        :data:`REVIEWER_LOCK_TTL` ago.

        Args:
            now: Optional reference time, primarily for tests.

        Returns:
            ``True`` if a live reviewer claim exists, otherwise ``False``.
        """
        if self.reviewer_locked_by_id is None or self.reviewer_locked_at is None:
            return False
        reference = now or timezone.now()
        return self.reviewer_locked_at > reference - REVIEWER_LOCK_TTL
