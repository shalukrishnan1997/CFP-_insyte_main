"""Banking models for the donation management system."""

from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from donations.models import Donation


class PayingInSlip(models.Model):
    """Daily paying-in slip for grouping physical payments for banking.

    Groups multiple supporter donations by client and payment type
    into a single paying-in slip for bank deposit reconciliation.

    Attributes:
        id: Auto-increment primary key.
        slip_number: Unique slip number for bank statement reconciliation.
        client: Client this slip belongs to.
        payment_type: Type of payments in this slip (cash, cheque, etc.).
        banking_date: Date the payments are being banked.
        total_amount: Total amount in the slip.
        total_items: Number of donations in the slip.
        status: Slip status (draft/ready/submitted_to_bank/processed/partially_processed/failed).
        notes: Optional notes about the slip.
        banked_at: Timestamp when slip was marked as banked.
        banked_by: User who marked the slip as banked.
        bank_processed_date: Date when bank actually processed the slip.
        processed_amount: Amount successfully processed by bank.
        completion_status: Processing outcome (full_success/partial_success/issues/failed).
        processing_issues: JSON array of issue codes encountered.
        custom_issue: User-entered issue description.
        processed_by: User who recorded bank processing results.
        processed_at: Timestamp when processing info was recorded.
        created_by: User who created the slip.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    PAYMENT_TYPE_CHOICES = [
        ("cash", "Cash"),
        ("cheque", "Cheque"),
        ("postal_order", "Postal Order"),
        ("caf", "CAF Voucher"),
        ("mixed", "Mixed"),
    ]

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("ready", "Ready for Banking"),
        ("submitted_to_bank", "Submitted to Bank"),
        ("processed", "Processed"),
        ("partially_processed", "Partially Processed"),
        ("failed", "Failed"),
        ("banked", "Banked"),  # Legacy status, kept for compatibility
    ]

    COMPLETION_STATUS_CHOICES = [
        ("full_success", "Fully Processed"),
        ("partial_success", "Partially Processed"),
        ("issues", "Processed with Issues"),
        ("failed", "Failed to Process"),
    ]

    PROCESSING_ISSUE_CHOICES = [
        ("insufficient_funds", "Insufficient Funds"),
        ("invalid_cheque", "Invalid/Bounced Cheque"),
        ("signature_mismatch", "Signature Mismatch"),
        ("expired_voucher", "Expired Voucher"),
        ("damaged_note", "Damaged Currency Note"),
        ("counterfeit_suspected", "Suspected Counterfeit"),
        ("account_closed", "Account Closed"),
        ("stop_payment", "Stop Payment"),
        ("incorrect_amount", "Incorrect Amount"),
        ("missing_items", "Missing Items"),
        ("other", "Other (see notes)"),
    ]

    id = models.AutoField(primary_key=True)
    slip_number = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Unique slip number for bank statement reconciliation",
    )
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="paying_in_slips",
        null=True,
        blank=True,
        help_text="Primary client (nullable for multi-client slips)",
    )
    payment_type = models.CharField(
        max_length=30,
        choices=PAYMENT_TYPE_CHOICES,
        default="mixed",
        help_text="Type of payments in this slip",
    )
    banking_date = models.DateField(
        db_index=True,
        help_text="Date the payments are being banked",
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total amount in the slip",
    )
    total_items = models.PositiveIntegerField(
        default=0,
        help_text="Number of donations in the slip",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
        db_index=True,
        help_text="Slip status",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional notes about the slip",
    )
    banked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When slip was marked as banked",
    )
    banked_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="banked_slips",
        help_text="User who marked the slip as banked",
    )
    bank_processed_date = models.DateField(
        null=True,
        blank=True,
        help_text="Actual date when bank processed the slip",
    )
    processed_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount successfully processed by bank",
    )
    completion_status = models.CharField(
        max_length=20,
        choices=COMPLETION_STATUS_CHOICES,
        blank=True,
        default="",
        help_text="Processing outcome status",
    )
    processing_issues = models.JSONField(
        default=list,
        blank=True,
        help_text="Array of processing issue codes",
    )
    custom_issue = models.TextField(
        blank=True,
        help_text="User-entered description of processing issues",
    )
    processed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_slips",
        help_text="User who recorded bank processing results",
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When processing information was recorded",
    )
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_paying_in_slips",
        help_text="User who created the slip",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        donations: RelatedManager[Donation]

    class Meta:
        ordering = ["-banking_date", "-created_at"]
        verbose_name = "Paying-In Slip"
        verbose_name_plural = "Paying-In Slips"
        indexes = [
            models.Index(fields=["client", "banking_date"]),
            models.Index(fields=["status", "banking_date"]),
        ]

    def __str__(self) -> str:
        client_name = self.client.name if self.client else "No Client"
        return f"{self.slip_number} - {client_name} ({self.banking_date})"

    def get_unprocessed_amount(self) -> Decimal:
        if self.processed_amount is None:
            return self.total_amount
        return self.total_amount - self.processed_amount

    def can_be_processed(self) -> bool:
        return self.status in ["ready", "submitted_to_bank"]
