"""Campaign models for the donation management system."""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from donations.models import Donation, DonationBatch


class PackageCode(models.Model):
    """Package codes for campaign categorization.

    Package codes can be associated with multiple campaigns for
    categorization and tracking purposes.

    Attributes:
        id: Auto-increment primary key.
        code: Unique package code string (indexed).
        description: Optional description of the package.
        is_active: Whether this package code is active.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        campaigns: Reverse many-to-many relation to Campaign model.
    """

    id = models.AutoField(primary_key=True)
    code = models.CharField(
        max_length=100, unique=True, db_index=True, help_text="Unique package code"
    )
    description = models.CharField(
        max_length=255, blank=True, help_text="Optional description of the package"
    )
    is_active = models.BooleanField(
        default=True, help_text="Is this package code active?"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        campaigns: RelatedManager[Campaign]

    class Meta:
        ordering = ["code"]
        verbose_name = "Package Code"
        verbose_name_plural = "Package Codes"

    def __str__(self) -> str:
        """Return string representation of the package code.

        Returns:
            str: The package code value.
        """
        return self.code


class Campaign(models.Model):
    """Donation campaign model.

    Manages donation campaigns with status tracking, dates, targets, and
    campaign-specific settings. Supports multiple package codes and custom fields.

    Attributes:
        id: UUID primary key.
        client: Foreign key to Client model.
        name: Campaign name (renamed from 'title' in migration 0008).
        description: Campaign description.
        status: Campaign status (draft/active/closed).
        allow_multiple_submissions: Whether multiple submissions allowed.
        require_authentication: Whether authentication required.
        open_date: Campaign open date.
        close_date: Campaign close date.
        appeal_code: Appeal code for tracking (indexed).
        package_code: Deprecated package code field.
        package_codes: Many-to-many relation to PackageCode.
        appeal_type: Type of appeal (Donation/Raffle).
        appeal_reference: Appeal reference string.
        appeal_start: Appeal start date.
        appeal_end: Appeal end date.
        created_by: User who created the campaign.
        start_date: Campaign start date.
        end_date: Campaign end date.
        target_amount: Target fundraising amount.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        fields: Reverse relation to CampaignField model.
        donations: Reverse relation to Donation model.
        donation_batches: Reverse relation to DonationBatch model.
        data_file: Reverse OneToOne relation to CampaignDataFile model.
    """

    # Status constants
    STATUS_DRAFT = "draft"
    STATUS_ACTIVE = "active"
    STATUS_LIVE = "active"  # Alias for STATUS_ACTIVE
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_CLOSED, "Closed"),
    ]

    # Donor source constants
    DONOR_SOURCE_HOUSE_FILE = "house_file"
    DONOR_SOURCE_DATA_FILE = "data_file"

    DONOR_SOURCE_CHOICES = [
        (DONOR_SOURCE_HOUSE_FILE, "House File"),
        (DONOR_SOURCE_DATA_FILE, "Data File"),
    ]

    # Campaign temperature constants — controls expected donor relationship flow
    CAMPAIGN_TEMPERATURE_WARM = "warm"
    CAMPAIGN_TEMPERATURE_COLD = "cold"

    CAMPAIGN_TEMPERATURE_CHOICES = [
        (CAMPAIGN_TEMPERATURE_WARM, "Warm"),
        (CAMPAIGN_TEMPERATURE_COLD, "Cold"),
    ]

    # Scan purpose constants — controls what happens after OCR extraction
    SCAN_PURPOSE_DONATION = "donation"
    SCAN_PURPOSE_DONOR_UPDATE = "donor_update"

    SCAN_PURPOSE_CHOICES = [
        (SCAN_PURPOSE_DONATION, "Donation — create donation records"),
        (
            SCAN_PURPOSE_DONOR_UPDATE,
            "Donor Update — update donor details only (no payment)",
        ),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="campaigns",
        help_text="Client/charity this campaign belongs to",
    )
    name = models.CharField(
        max_length=255
    )  # This was 'title' but we renamed it in migration 0008
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT
    )

    # Original fields that still exist in database
    allow_multiple_submissions = models.BooleanField(default=True)
    require_authentication = models.BooleanField(default=False)
    appeal_code = models.CharField(
        max_length=100, blank=True, default="", db_index=True
    )
    package_code = models.CharField(
        max_length=100, blank=True, default=""
    )  # Deprecated: kept for migration
    package_codes = models.ManyToManyField(PackageCode, related_name="campaigns")
    appeal_type = models.CharField(
        max_length=32,
        choices=[("Donation", "Donation"), ("Raffle", "Raffle")],
        default="Donation",
    )
    appeal_start = models.DateField(default=timezone.now)
    appeal_end = models.DateField(default=timezone.now)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_campaigns",
    )

    # HGV/LGV Fields
    hgv_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="High Gift Value threshold",
    )
    lgv_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Low Gift Value threshold",
    )
    campaign_manager_emails = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated list of email addresses to notify for HGV donations",
    )

    # Donor source configuration
    donor_source = models.CharField(
        max_length=20,
        choices=DONOR_SOURCE_CHOICES,
        default=DONOR_SOURCE_HOUSE_FILE,
        help_text="Donor lookup source: House File (global donors) or Data File (campaign-specific donors)",
    )

    campaign_temperature = models.CharField(
        max_length=20,
        choices=CAMPAIGN_TEMPERATURE_CHOICES,
        default=CAMPAIGN_TEMPERATURE_COLD,
        db_index=True,
        help_text=(
            "Campaign type: Warm campaigns target known supporters and expect "
            "QR-backed forms whose donors already exist in the configured "
            "source. Cold campaigns handle handwritten or new-supporter forms "
            "where QR is not expected and new donor creation is allowed."
        ),
    )

    scan_purpose = models.CharField(
        max_length=20,
        choices=SCAN_PURPOSE_CHOICES,
        default=SCAN_PURPOSE_DONATION,
        db_index=True,
        help_text=(
            "What to do with scanned data: 'donation' creates donation records; "
            "'donor_update' updates donor contact details and creates a "
            "non-financial donation as an audit trail."
        ),
    )

    # OCR confidence threshold — donations whose critical OCR fields fall below
    # this score are flagged for mandatory per-donation review and are excluded
    # from the batch-approval auto-approve cascade. Configurable per campaign so
    # high-stakes appeals can demand stricter QA than routine batches. Stored as
    # a Decimal to avoid float drift on the boundary value.
    ocr_confidence_threshold = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=Decimal("0.700"),
        help_text=(
            "Per-field OCR confidence floor (0.000-1.000). Donations whose "
            "amount or date confidence falls below this threshold are flagged "
            "for mandatory QA review and cannot be auto-approved on batch "
            "approval. Default is 0.700."
        ),
    )

    # New fields added in migration 0008
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    target_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Payment Processing Fields
    payment_eligible = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Campaign is eligible for payment processing (all batches QA approved)",
    )
    payment_status = models.CharField(
        max_length=30,
        choices=[
            ("not_started", "Not Started"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
            ("partially_completed", "Partially Completed"),
            ("failed", "Failed"),
        ],
        default="not_started",
        db_index=True,
        help_text="Overall payment status for campaign",
    )
    payment_processed_at = models.DateTimeField(
        null=True, blank=True, help_text="When payment processing was completed"
    )
    payment_processed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_campaigns",
        help_text="User who processed campaign payments",
    )
    total_payment_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total amount of credit card payments in campaign",
    )
    successful_payments = models.IntegerField(
        default=0, help_text="Count of successful payments"
    )
    failed_payments = models.IntegerField(
        default=0, help_text="Count of failed payments"
    )

    # ─── Soft archive (audit 2026-05-02 §4.1) ──────────────────────────
    # Campaigns may not be hard-deleted: every cross-app FK uses
    # ``on_delete=PROTECT`` so a delete with linked rows raises. To retire
    # an inactive charity / appeal, set ``is_archived=True``.
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Archived campaigns are hidden from active lists and "
        "cannot accept new donations.",
    )
    archived_at = models.DateTimeField(
        null=True, blank=True, help_text="When the campaign was archived."
    )

    if TYPE_CHECKING:
        fields: RelatedManager[CampaignField]
        donations: RelatedManager[Donation]
        donation_batches: RelatedManager[DonationBatch]
        data_file: CampaignDataFile

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("manage_campaigns", "Can manage campaigns (create/edit forms)"),
            ("fill_donations", "Can fill donation forms"),
        ]

    def __str__(self) -> str:
        """Return string representation of the campaign.

        Returns:
            str: Campaign name with client name in parentheses.
        """
        return f"{self.name} ({self.client.name})"

    def clean(self) -> None:
        """Validate campaign integrity.

        Ensures that the campaign is attached to a client and that end_date is
        not before start_date.

        Raises:
            ValidationError: If required campaign data is invalid.
        """
        if self.client_id is None:
            raise ValidationError("Campaign must be attached to a client")
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError("End date cannot be before start date")

    def archive(self) -> None:
        """Mark the campaign as archived. Idempotent."""
        from django.utils import timezone

        if self.is_archived:
            return
        self.is_archived = True
        self.archived_at = timezone.now()
        self.save(update_fields=["is_archived", "archived_at", "updated_at"])

    def unarchive(self) -> None:
        """Reverse ``archive()`` — bring a campaign back into active rotation."""
        if not self.is_archived:
            return
        self.is_archived = False
        self.archived_at = None
        self.save(update_fields=["is_archived", "archived_at", "updated_at"])

    @property
    def is_closed(self) -> bool:
        """Check if campaign is closed.

        Returns:
            bool: True if campaign status is closed, False otherwise.
        """
        return self.status == self.STATUS_CLOSED

    @property
    def is_active(self) -> bool:
        """Check if campaign is active.

        Returns:
            bool: True if campaign status is active, False otherwise.
        """
        return self.status == self.STATUS_ACTIVE

    @property
    def can_upload_donors(self) -> bool:
        """Check if bulk donor upload is allowed.

        Returns:
            bool: True if campaign is not closed, False if closed.
        """
        return self.status != self.STATUS_CLOSED


class CampaignField(models.Model):
    """Dynamic custom fields for campaign forms.

    Allows campaigns to define custom form fields with various input types.
    Fields can be required, have options, and are ordered for display.

    Attributes:
        id: UUID primary key.
        campaign: Foreign key to Campaign model.
        label: Field label displayed to users.
        field_type: Type of input field (text, number, email, etc.).
        placeholder: Placeholder text for the field.
        help_text: Help text shown below the field.
        required: Whether the field is required.
        is_default_field: Whether this is a default field (cannot be deleted).
        options: JSON array of options for dropdown/radio/checkbox fields.
        order: Display order (lower numbers appear first).
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    FIELD_TEXT = "text"
    FIELD_NUMBER = "number"
    FIELD_EMAIL = "email"
    FIELD_PHONE = "phone"
    FIELD_DATE = "date"
    FIELD_DROPDOWN = "dropdown"
    FIELD_RADIO = "radio"
    FIELD_CHECKBOX = "checkbox"
    FIELD_TEXTAREA = "textarea"
    FIELD_FILE = "file"

    FIELD_TYPE_CHOICES = [
        (FIELD_TEXT, "Text Input"),
        (FIELD_NUMBER, "Number Input"),
        (FIELD_EMAIL, "Email Input"),
        (FIELD_PHONE, "Phone Input"),
        (FIELD_DATE, "Date Input"),
        (FIELD_DROPDOWN, "Dropdown Select"),
        (FIELD_RADIO, "Radio Buttons"),
        (FIELD_CHECKBOX, "Checkboxes"),
        (FIELD_TEXTAREA, "Text Area"),
        (FIELD_FILE, "File Upload"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="fields"
    )

    label = models.CharField(
        max_length=255, help_text="Field label (e.g., 'Donor Name')"
    )
    field_type = models.CharField(
        max_length=20, choices=FIELD_TYPE_CHOICES, default=FIELD_TEXT
    )
    placeholder = models.CharField(max_length=255, blank=True)
    help_text = models.CharField(max_length=500, blank=True)

    # Validation
    required = models.BooleanField(default=False)
    is_default_field = models.BooleanField(
        default=False, help_text="Default field that cannot be deleted"
    )

    # For dropdown/radio/checkbox options (JSON array)
    options = models.JSONField(
        default=list, blank=True, help_text="Options for dropdown/radio/checkbox"
    )

    # Display order
    order = models.PositiveIntegerField(default=0)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "label"],
                name="unique_campaign_field_label",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation of the campaign field.

        Returns:
            str: Campaign name, field label, and field type.
        """
        return f"{self.campaign.name} - {self.label} ({self.get_field_type_display()})"


class CampaignDataFile(models.Model):
    """Campaign-specific donor data file.

    Contains donors specific to a single campaign for faster lookup.
    Has a one-to-one relationship with Campaign.

    Attributes:
        campaign: OneToOne primary key to Campaign model.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        created_by: User who created this data file.
        total_donors: Total number of donors in this data file.
        donors: Reverse relation to DataFileDonor model.
        uploads: Reverse relation to DataFileUpload model.
    """

    campaign = models.OneToOneField(
        "campaigns.Campaign",
        on_delete=models.CASCADE,
        related_name="data_file",
        primary_key=True,
        help_text="Campaign this data file belongs to",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_data_files",
    )

    # Statistics
    total_donors = models.PositiveIntegerField(
        default=0, help_text="Total number of donors in this data file"
    )

    if TYPE_CHECKING:
        from donors.models import DataFileDonor

        donors: RelatedManager[DataFileDonor]
        uploads: RelatedManager["DataFileUpload"]  # noqa: UP037

    class Meta:
        verbose_name = "Campaign Data File"
        verbose_name_plural = "Campaign Data Files"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        """Return string representation of the campaign data file.

        Returns:
            str: Description with campaign name.
        """
        return f"Data File for {self.campaign.name}"


class DataFileUpload(models.Model):
    """Bulk donor upload tracking for data files.

    Tracks file uploads of donors to campaign data files with import statistics.

    Attributes:
        id: UUID primary key.
        data_file: Foreign key to CampaignDataFile.
        file: Uploaded CSV/Excel file.
        uploaded_by: User who uploaded the file.
        total_rows: Total rows in the uploaded file.
        successful_imports: Number of successfully imported donors.
        failed_imports: Number of failed imports.
        error_log: JSON array of error messages.
        status: Upload status (pending/processing/completed/failed).
        created_at: Timestamp when uploaded.
        completed_at: Timestamp when processing completed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    data_file = models.ForeignKey(
        CampaignDataFile,
        on_delete=models.CASCADE,
        related_name="uploads",
        help_text="Data file this upload belongs to",
    )
    file = models.FileField(
        upload_to="uploads/datafile_donors/", help_text="CSV/Excel file with donor data"
    )
    uploaded_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="datafile_uploads",
    )

    # Upload statistics
    total_rows = models.PositiveIntegerField(default=0, help_text="Total rows in file")
    successful_imports = models.PositiveIntegerField(
        default=0, help_text="Successfully imported"
    )
    failed_imports = models.PositiveIntegerField(default=0, help_text="Failed imports")
    error_log = models.JSONField(
        default=list, blank=True, help_text="List of errors encountered during import"
    )

    # Status
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Data File Upload"
        verbose_name_plural = "Data File Uploads"

    def __str__(self) -> str:
        """Return string representation of data file upload.

        Returns:
            str: Description with campaign name, uploader, and status.
        """
        uploader = self.uploaded_by.get_username() if self.uploaded_by else "system"
        return (
            f"Upload for {self.data_file.campaign.name} by {uploader} ({self.status})"
        )
