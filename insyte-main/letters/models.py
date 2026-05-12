"""Letter template and batch models for the donation management system."""

import uuid
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Q

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from donations.models import Donation


class LetterTemplate(models.Model):
    """DOCX template (thank-you or issue) for a campaign.

    Each campaign has at most one active template of each type at any time.
    Uploading a new template of the same type deactivates the previous one.
    """

    TEMPLATE_TYPE_THANK_YOU = "thank_you"
    TEMPLATE_TYPE_ISSUE = "issue"
    TEMPLATE_TYPE_CHOICES = [
        (TEMPLATE_TYPE_THANK_YOU, "Thank You Letter"),
        (TEMPLATE_TYPE_ISSUE, "Issue Letter"),
    ]

    name = models.CharField(
        max_length=255,
        default="Template",
        help_text="User-defined name for this template",
    )
    template_type = models.CharField(
        max_length=20,
        choices=TEMPLATE_TYPE_CHOICES,
        default=TEMPLATE_TYPE_THANK_YOU,
        db_index=True,
        help_text=(
            "Thank-you letters go to QA-approved donations; "
            "issue letters go to QA-rejected donations."
        ),
    )
    # PROTECT (audit 2026-05-02 §4.1): templates outlive their campaigns.
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, related_name="letter_templates"
    )
    file = models.FileField(upload_to="uploads/letter_templates/")
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Only the active template for each (campaign, type) is used for "
            "new letter batches."
        ),
    )
    created_by = models.ForeignKey("core.User", on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "template_type"],
                condition=Q(is_active=True),
                name="unique_active_template_per_campaign_type",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.name} ({self.get_template_type_display()}) - {self.campaign.name}"
        )


class LetterBatch(models.Model):
    """Batch of generated letters for a campaign."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    FILTER_ALL = "all"
    FILTER_EXCLUDE_LGV = "exclude_lgv"
    FILTER_ONLY_HGV = "only_hgv"
    FILTER_CHOICES = [
        (FILTER_ALL, "All Donations"),
        (FILTER_EXCLUDE_LGV, "Exclude LGV"),
        (FILTER_ONLY_HGV, "Only HGV"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT (audit 2026-05-02 §4.1): batches reference issued letters.
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, related_name="letter_batches"
    )
    template = models.ForeignKey(
        LetterTemplate,
        on_delete=models.CASCADE,
        related_name="letter_batches",
        help_text="Thank-you template used for approved donations in this batch.",
    )
    failure_template = models.ForeignKey(
        LetterTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="failure_letter_batches",
        help_text=(
            "Issue template used for rejected donations. If blank, rejected "
            "donations are skipped."
        ),
    )
    batch_number = models.PositiveIntegerField(
        help_text="Sequential batch number for this campaign"
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    # Progress tracking
    total_letters = models.PositiveIntegerField(
        default=0, help_text="Total number of letters to generate in this batch"
    )
    generated_count = models.PositiveIntegerField(
        default=0, help_text="Number of letters successfully generated"
    )
    failed_count = models.PositiveIntegerField(
        default=0, help_text="Number of letters that failed to generate"
    )
    progress_percent = models.PositiveIntegerField(
        default=0, help_text="Generation progress percentage (0-100)"
    )

    # File configuration
    letters_per_file = models.PositiveIntegerField(
        default=100, help_text="Number of letters per merged document file"
    )
    output_files = models.JSONField(
        default=list,
        blank=True,
        help_text="List of generated file paths",
    )
    file_count = models.PositiveIntegerField(
        default=0, help_text="Number of output files generated"
    )

    donation_filter = models.CharField(
        max_length=20,
        choices=FILTER_CHOICES,
        default=FILTER_ALL,
        help_text="Filter donations by gift value (HGV/LGV)",
    )
    regenerate_mode = models.BooleanField(
        default=False,
        help_text=(
            "If True, regenerate letters for donations that already have "
            "generated letters. If False, only process donations whose "
            "letter_status is pending."
        ),
    )

    # Error tracking (capped at 100 messages in _complete_batch)
    error_log = models.JSONField(
        default=list, blank=True, help_text="List of errors encountered"
    )

    # Celery task tracking
    celery_task_id = models.CharField(
        max_length=255, blank=True, help_text="Celery task ID for tracking progress"
    )

    # Timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="letter_batches_created",
    )

    if TYPE_CHECKING:
        donations: RelatedManager[Donation]

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Letter Batch"
        verbose_name_plural = "Letter Batches"
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "batch_number"],
                name="unique_campaign_batch_number",
            )
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return (
            f"Letter Batch #{self.batch_number} - {self.campaign.name} ({self.status})"
        )
