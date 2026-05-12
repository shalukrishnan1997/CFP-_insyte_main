"""Donor models for the donation management system."""

import uuid
from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from clients.models import Client
    from donations.models import Donation
    from donors.models import DataFileDonor


class Donor(models.Model):
    """Imported house-file donor model.

    Source donor table populated from the latest uploaded house file for a
    client. These rows are replaceable import inputs and are not intended to be
    the permanent system-of-record for processed donation history.
    Includes personal info, address, GDPR consent, and opt-in preferences.

    Attributes:
        id: UUID primary key (system-generated, never changes).
        client: Charity/client this donor belongs to.
        urn: Unique Reference Number from the charity (nullable until assigned).
        title: Salutation (Mr, Mrs, Ms, etc.).
        first_name: Donor's first name.
        last_name: Donor's last name.
        email: Email address.
        phone: Phone number.
        address_line1: First line of address.
        address_line2: Second line of address.
        city: City name.
        county: County name.
        postcode: UK postcode.
        country: Country name.
        date_of_birth: Date of birth (optional).
        age: Age in years (optional).
        gift_aid_declaration: Whether the donor has made a Gift Aid declaration.
        gift_aid_date: Date the Gift Aid declaration was made.
        verification_status: Whether donor is verified, unverified, or pending export.
        contact_status: Operational contactability state for postal follow-up.
        contact_status_reason: Reason or source for the current contact status.
        contact_status_changed_at: When the current contact status was last changed.
        address_last_verified_at: When the donor address was last confirmed.
        consent_contact: Whether donor consents to being contacted.
        opt_in_email: Whether donor opted in for email contact.
        opt_in_sms: Whether donor opted in for SMS contact.
        opt_in_phone: Whether donor opted in for phone contact.
        opt_in_post: Whether donor opted in for postal contact.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        created_by: User who created this donor record.
        donations: Reverse relation to Donation model.
        data_file_entries: Reverse relation to DataFileDonor model.
    """

    # Verification status constants
    VERIFICATION_VERIFIED = "verified"
    VERIFICATION_UNVERIFIED = "unverified"
    VERIFICATION_PENDING_EXPORT = "pending_export"
    VERIFICATION_STATUS_CHOICES = [
        (VERIFICATION_VERIFIED, "Verified"),
        (VERIFICATION_UNVERIFIED, "Unverified"),
        (VERIFICATION_PENDING_EXPORT, "Pending Export"),
    ]

    CONTACT_STATUS_NORMAL = "normal"
    CONTACT_STATUS_GONE_AWAY = "gone_away"
    CONTACT_STATUS_DECEASED = "deceased"
    CONTACT_STATUS_BAD_ADDRESS = "bad_address"
    CONTACT_STATUS_TEMPORARILY_SUPPRESSED = "temporarily_suppressed"
    CONTACT_STATUS_CHOICES = [
        (CONTACT_STATUS_NORMAL, "Normal"),
        (CONTACT_STATUS_GONE_AWAY, "Gone Away"),
        (CONTACT_STATUS_DECEASED, "Deceased"),
        (CONTACT_STATUS_BAD_ADDRESS, "Bad Address"),
        (CONTACT_STATUS_TEMPORARILY_SUPPRESSED, "Temporarily Suppressed"),
    ]
    POSTAL_SUPPRESSED_CONTACT_STATUSES = (
        CONTACT_STATUS_GONE_AWAY,
        CONTACT_STATUS_DECEASED,
        CONTACT_STATUS_BAD_ADDRESS,
        CONTACT_STATUS_TEMPORARILY_SUPPRESSED,
    )

    TITLE_CHOICES = [
        ("Mr", "Mr"),
        ("Mrs", "Mrs"),
        ("Ms", "Ms"),
        ("Miss", "Miss"),
        ("Dr", "Dr"),
        ("Prof", "Prof"),
        ("Rev", "Rev"),
        ("Sir", "Sir"),
        ("Lady", "Lady"),
        ("Other", "Other"),
    ]

    # UUID primary key (system-generated, stable identifier)
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="System-generated unique identifier for the donor",
    )

    client = models.ForeignKey(  # pyright: ignore[reportAssignmentType]
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="donors",
        null=True,
        blank=True,
        help_text="Client/charity this donor belongs to",
    )

    # URN from the charity (nullable until assigned via house file reconciliation)
    urn = models.CharField(  # noqa: DJ001
        max_length=50,
        null=True,
        blank=True,
        help_text=(
            "Unique Reference Number from the charity. Null for new donors "
            "captured before the charity assigns a URN."
        ),
    )

    # Personal Information
    title = models.CharField(max_length=10, choices=TITLE_CHOICES, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, help_text="Email address")
    phone = models.CharField(max_length=20, blank=True, help_text="Phone number")

    # Address Information
    address_line1 = models.CharField(
        max_length=255, blank=True, help_text="Address Line 1"
    )
    address_line2 = models.CharField(
        max_length=255, blank=True, help_text="Address Line 2"
    )
    city = models.CharField(max_length=100, blank=True)
    county = models.CharField(max_length=100, blank=True)
    postcode = models.CharField(max_length=20, blank=True, help_text="UK Postcode")
    country = models.CharField(max_length=100, default="United Kingdom")

    # Optional Information
    date_of_birth = models.DateField(null=True, blank=True, help_text="Date of birth")
    age = models.PositiveIntegerField(
        null=True, blank=True, help_text="Age (if DOB not provided)"
    )
    gift_aid_declaration = models.BooleanField(
        default=False,
        help_text="Whether the donor has made a Gift Aid declaration",
    )
    gift_aid_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the donor made their Gift Aid declaration",
    )

    # Verification Status
    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default=VERIFICATION_VERIFIED,
        help_text=(
            "Verified: confirmed donor in the house file. "
            "Unverified: newly created during data-file campaign entry, "
            "not yet confirmed. "
            "Pending Export: created during cold campaign entry for a donor "
            "not in the house file — reference to be exported to the charity."
        ),
    )
    contact_status = models.CharField(
        max_length=30,
        choices=CONTACT_STATUS_CHOICES,
        default=CONTACT_STATUS_NORMAL,
        help_text=(
            "Operational contactability status for donor servicing and "
            "postal suppression."
        ),
    )
    contact_status_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason or source for the donor's current contact status",
    )
    contact_status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor's current contact status was last changed",
    )
    address_last_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor address was last confirmed or corrected",
    )

    # GDPR Consent
    consent_contact = models.BooleanField(
        default=False, help_text="Consents to being contacted by charity"
    )

    # Opt-in Preferences
    opt_in_email = models.BooleanField(
        default=False, help_text="Opt-in for email contact"
    )
    opt_in_sms = models.BooleanField(default=False, help_text="Opt-in for SMS contact")
    opt_in_phone = models.BooleanField(
        default=False, help_text="Opt-in for phone contact"
    )
    opt_in_post = models.BooleanField(
        default=False, help_text="Opt-in for postal contact"
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, null=True, related_name="donors_created"
    )

    if TYPE_CHECKING:
        client: Client | None
        donations: RelatedManager[Donation]
        data_file_entries: RelatedManager[DataFileDonor]

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["client", "urn"]),
            models.Index(fields=["urn"]),
            models.Index(fields=["last_name", "first_name"]),
            models.Index(fields=["email"]),
            models.Index(fields=["postcode"]),
            models.Index(fields=["verification_status"]),
            models.Index(fields=["contact_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "urn"],
                name="unique_donor_urn_per_client",
            )
        ]
        verbose_name = "donors.Donor"
        verbose_name_plural = "Donors"

    def __str__(self) -> str:
        """Return string representation of the donor.

        Returns:
            str: Full name with title and URN (or 'New Donor') in parentheses.
        """
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        urn_label = self.urn or "New Donor — Pending URN"
        return f"{' '.join(name_parts)} ({urn_label})"

    @property
    def is_pending_urn(self) -> bool:
        """Return True if this donor does not yet have a URN from the charity.

        Returns:
            bool: True when ``urn`` is None (donor captured before URN assigned).
        """
        return self.urn is None

    @property
    def full_name(self) -> str:
        """Return donor's full name with title.

        Returns:
            str: Title (if present), first name, and last name joined by spaces.
        """
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        return " ".join(name_parts)

    @property
    def full_address(self) -> str:
        """Return formatted comma-separated address.

        Returns:
            str: All address components joined by commas, omitting empty fields.
        """
        parts = [
            self.address_line1,
            self.address_line2,
            self.city,
            self.county,
            self.postcode,
            self.country,
        ]
        return ", ".join([p for p in parts if p])

    @property
    def is_postal_contact_suppressed(self) -> bool:
        """Return whether this donor should be excluded from postal outreach."""
        return self.contact_status in self.POSTAL_SUPPRESSED_CONTACT_STATUSES


class DonorUpload(models.Model):
    """Bulk donor upload tracking to house file.

    Tracks uploads of donors to the main house file (not data files).

    Attributes:
        id: UUID primary key.
        campaign: Campaign associated with this upload.
        file: Uploaded file.
        uploaded_by: User who uploaded the file.
        created_at: Timestamp when uploaded.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey("campaigns.Campaign", on_delete=models.CASCADE)
    file = models.FileField(upload_to="uploads/donors/")
    uploaded_by = models.ForeignKey("core.User", on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return string representation of donor upload.

        Returns:
            str: Upload ID, campaign, uploader, and date.
        """
        uploader = self.uploaded_by.get_username() if self.uploaded_by else "system"
        return f"DonorUpload {self.id} for {self.campaign.name} by {uploader} ({self.created_at.date()})"


class SystemDonor(models.Model):
    """Internal donor profile used as the permanent system-of-record.

    Unlike house-file and data-file source rows, this model stores the
    application-owned donor profile that persists across future source-file
    replacements. Donation processing upserts into this model so financial and
    non-financial history remain stable even when source imports are replaced.
    """

    TITLE_CHOICES = Donor.TITLE_CHOICES
    CONTACT_STATUS_CHOICES = Donor.CONTACT_STATUS_CHOICES
    CONTACT_STATUS_NORMAL = Donor.CONTACT_STATUS_NORMAL
    POSTAL_SUPPRESSED_CONTACT_STATUSES = Donor.POSTAL_SUPPRESSED_CONTACT_STATUSES

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="System-generated unique identifier for the internal donor profile",
    )
    client = models.ForeignKey(  # pyright: ignore[reportAssignmentType]
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="system_donors",
        null=True,
        blank=True,
        help_text="Client/charity this internal donor belongs to",
    )
    external_urn = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Latest known source-system URN for this donor, when available",
    )
    title = models.CharField(max_length=10, choices=TITLE_CHOICES, blank=True)
    first_name = models.CharField(max_length=100, help_text="Donor first name")
    last_name = models.CharField(max_length=100, help_text="Donor last name")
    email = models.EmailField(blank=True, help_text="Email address")
    phone = models.CharField(max_length=20, blank=True, help_text="Phone number")
    normalized_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Digits-only canonical form of phone, populated from phone on save. "
            "Used by the donor-search API so operator-typed variants "
            "(e.g. '07700 900 123' vs '07700900123') match the same donor."
        ),
    )
    address_line1 = models.CharField(
        max_length=255, blank=True, help_text="Address Line 1"
    )
    address_line2 = models.CharField(
        max_length=255, blank=True, help_text="Address Line 2"
    )
    city = models.CharField(max_length=100, blank=True, help_text="City")
    county = models.CharField(max_length=100, blank=True, help_text="County")
    postcode = models.CharField(max_length=20, blank=True, help_text="UK Postcode")
    country = models.CharField(
        max_length=100,
        default="United Kingdom",
        help_text="Country",
    )
    date_of_birth = models.DateField(null=True, blank=True, help_text="Date of birth")
    age = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Age (if date of birth is not available)",
    )
    gift_aid_declaration = models.BooleanField(
        default=False,
        help_text="Whether the donor has made a Gift Aid declaration",
    )
    gift_aid_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the donor made their Gift Aid declaration",
    )
    contact_status = models.CharField(
        max_length=30,
        choices=CONTACT_STATUS_CHOICES,
        default=CONTACT_STATUS_NORMAL,
        help_text="Operational contactability status for donor servicing",
    )
    contact_status_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason or source for the donor's current contact status",
    )
    contact_status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor's current contact status was last changed",
    )
    address_last_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor address was last confirmed or corrected",
    )
    consent_contact = models.BooleanField(
        default=False,
        help_text="Consents to being contacted by charity",
    )
    opt_in_email = models.BooleanField(
        default=False,
        help_text="Opt-in for email contact",
    )
    opt_in_sms = models.BooleanField(default=False, help_text="Opt-in for SMS contact")
    opt_in_phone = models.BooleanField(
        default=False,
        help_text="Opt-in for phone contact",
    )
    opt_in_post = models.BooleanField(
        default=False,
        help_text="Opt-in for postal contact",
    )
    source_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Latest imported or OCR-derived donor snapshot used for upsert",
    )
    pending_review = models.BooleanField(
        default=False,
        help_text=(
            "True when the donor was auto-created by scan pipeline and not yet "
            "confirmed by QA."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="system_donors_created",
        help_text="User who first created this internal donor profile",
    )

    if TYPE_CHECKING:
        client: Client | None
        donations: RelatedManager[Donation]

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["client", "external_urn"]),
            models.Index(fields=["client", "last_name", "first_name"]),
            models.Index(fields=["client", "postcode"]),
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["postcode"]),
            models.Index(fields=["contact_status"]),
            models.Index(fields=["pending_review"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "external_urn"],
                name="unique_system_donor_external_urn_per_client",
            )
        ]
        verbose_name = "System Donor"
        verbose_name_plural = "System Donors"

    def __str__(self) -> str:
        """Return string representation of the internal donor."""
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        urn_label = self.external_urn or "Internal Donor"
        return f"{' '.join(name_parts)} ({urn_label})"

    def save(self, *args: object, **kwargs: object) -> None:
        """Persist *self* with a freshly computed ``normalized_phone``."""
        from core.services.phone import normalize_phone

        self.normalized_phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    @property
    def full_name(self) -> str:
        """Return donor's full name with title."""
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        return " ".join(name_parts)

    @property
    def full_address(self) -> str:
        """Return formatted comma-separated address."""
        parts = [
            self.address_line1,
            self.address_line2,
            self.city,
            self.county,
            self.postcode,
            self.country,
        ]
        return ", ".join([p for p in parts if p])

    @property
    def is_postal_contact_suppressed(self) -> bool:
        """Return whether this donor should be excluded from postal outreach."""
        return self.contact_status in self.POSTAL_SUPPRESSED_CONTACT_STATUSES


class Segment(models.Model):
    """Donor segmentation for campaigns.

    Groups donors into segments for targeted campaigns.

    Attributes:
        id: UUID primary key.
        campaign: Campaign this segment belongs to.
        name: Segment name.
        created_at: Timestamp when created.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey("campaigns.Campaign", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return string representation of segment.

        Returns:
            str: Segment name and campaign name.
        """
        return f"{self.name} ({self.campaign.name})"


class DataFileDonor(models.Model):
    """Campaign-specific donor record from data file.

    Lighter version of Donor model for campaign-specific lookups.
    URN is unique per data file, not globally unique.

    Attributes:
        id: UUID primary key.
        data_file: Foreign key to CampaignDataFile.
        client: Charity/client this donor belongs to.
        urn: URN unique within this data file.
        title: Salutation.
        first_name: First name.
        last_name: Last name.
        email: Email address.
        phone: Phone number.
        address_line1: First line of address.
        address_line2: Second line of address.
        city: City name.
        county: County name.
        postcode: Postcode.
        country: Country name.
        date_of_birth: Date of birth.
        age: Age in years.
        gift_aid_declaration: Whether the donor has made a Gift Aid declaration.
        gift_aid_date: Date the Gift Aid declaration was made.
        contact_status: Operational contactability state for postal follow-up.
        contact_status_reason: Reason or source for the current contact status.
        contact_status_changed_at: When the current contact status was last changed.
        address_last_verified_at: When the donor address was last confirmed.
        consent_contact: Contact consent flag.
        no_thank_you: Opt-out of thank-you communications.
        opt_in_email: Email opt-in.
        opt_in_sms: SMS opt-in.
        opt_in_phone: Phone opt-in.
        opt_in_post: Post opt-in.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        created_by: User who created this record.
        house_file_donor: Optional link to main Donor record.
        donations: Reverse relation to Donation model.
    """

    TITLE_CHOICES = [
        ("Mr", "Mr"),
        ("Mrs", "Mrs"),
        ("Ms", "Ms"),
        ("Miss", "Miss"),
        ("Dr", "Dr"),
        ("Prof", "Prof"),
        ("Rev", "Rev"),
        ("Sir", "Sir"),
        ("Lady", "Lady"),
        ("Other", "Other"),
    ]
    CONTACT_STATUS_NORMAL = "normal"
    CONTACT_STATUS_GONE_AWAY = "gone_away"
    CONTACT_STATUS_DECEASED = "deceased"
    CONTACT_STATUS_BAD_ADDRESS = "bad_address"
    CONTACT_STATUS_TEMPORARILY_SUPPRESSED = "temporarily_suppressed"
    CONTACT_STATUS_CHOICES = [
        (CONTACT_STATUS_NORMAL, "Normal"),
        (CONTACT_STATUS_GONE_AWAY, "Gone Away"),
        (CONTACT_STATUS_DECEASED, "Deceased"),
        (CONTACT_STATUS_BAD_ADDRESS, "Bad Address"),
        (CONTACT_STATUS_TEMPORARILY_SUPPRESSED, "Temporarily Suppressed"),
    ]
    POSTAL_SUPPRESSED_CONTACT_STATUSES = (
        CONTACT_STATUS_GONE_AWAY,
        CONTACT_STATUS_DECEASED,
        CONTACT_STATUS_BAD_ADDRESS,
        CONTACT_STATUS_TEMPORARILY_SUPPRESSED,
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link to data file (and indirectly to campaign)
    data_file = models.ForeignKey(
        "campaigns.CampaignDataFile",
        on_delete=models.CASCADE,
        related_name="donors",
        help_text="Data file this donor belongs to",
    )

    client = models.ForeignKey(  # pyright: ignore[reportAssignmentType]
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="data_file_donors",
        null=True,
        blank=True,
        help_text="Client/charity this donor belongs to",
    )

    # URN for this campaign-specific donor
    urn = models.CharField(
        max_length=50,
        help_text="Unique Reference Number for the donor in this campaign",
    )

    package_code = models.CharField(
        max_length=100,
        blank=True,
        help_text="Package code assigned to the donor for this campaign",
    )

    # Personal Information
    title = models.CharField(max_length=10, choices=TITLE_CHOICES, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)

    # Address Information
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    county = models.CharField(max_length=100, blank=True)
    postcode = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, default="United Kingdom")

    # Optional Information
    date_of_birth = models.DateField(null=True, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    gift_aid_declaration = models.BooleanField(
        default=False,
        help_text="Whether the donor has made a Gift Aid declaration",
    )
    gift_aid_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the donor made their Gift Aid declaration",
    )

    contact_status = models.CharField(
        max_length=30,
        choices=CONTACT_STATUS_CHOICES,
        default=CONTACT_STATUS_NORMAL,
        help_text=(
            "Operational contactability status for donor servicing and "
            "postal suppression."
        ),
    )
    contact_status_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason or source for the donor's current contact status",
    )
    contact_status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor's current contact status was last changed",
    )
    address_last_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the donor address was last confirmed or corrected",
    )

    # GDPR Consent
    consent_contact = models.BooleanField(default=False)
    no_thank_you = models.BooleanField(
        default=False,
        help_text="Donor has opted out of thank-you communications.",
    )
    opt_in_email = models.BooleanField(default=False)
    opt_in_sms = models.BooleanField(default=False)
    opt_in_phone = models.BooleanField(default=False)
    opt_in_post = models.BooleanField(default=False)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="datafile_donors_created",
    )

    # Optional link to main house file donor (if they exist in main donor table)
    house_file_donor = models.ForeignKey(
        "donors.Donor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="data_file_entries",
        help_text="Link to house file donor if exists",
    )

    if TYPE_CHECKING:
        client: Client | None
        donations: RelatedManager[Donation]

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["client", "urn"]),
            models.Index(fields=["data_file", "urn"]),
            models.Index(
                fields=["package_code"],
                name="core_datafi_package_c28584_idx",
            ),
            models.Index(fields=["last_name", "first_name"]),
            models.Index(fields=["email"]),
            models.Index(fields=["postcode"]),
            models.Index(fields=["contact_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["data_file", "urn"], name="unique_urn_per_datafile"
            )
        ]
        verbose_name = "Data File Donor"
        verbose_name_plural = "Data File Donors"

    def __str__(self) -> str:
        """Return string representation of data file donor.

        Returns:
            str: Full name with URN in parentheses.
        """
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        return f"{' '.join(name_parts)} ({self.urn})"

    @property
    def full_name(self) -> str:
        """Return donor's full name with title.

        Returns:
            str: Title (if present), first name, and last name.
        """
        name_parts = []
        if self.title:
            name_parts.append(self.title)
        name_parts.extend([self.first_name, self.last_name])
        return " ".join(name_parts)

    @property
    def full_address(self) -> str:
        """Return formatted comma-separated address.

        Returns:
            str: All address components joined by commas.
        """
        parts = [
            self.address_line1,
            self.address_line2,
            self.city,
            self.county,
            self.postcode,
            self.country,
        ]
        return ", ".join([p for p in parts if p])

    @property
    def is_postal_contact_suppressed(self) -> bool:
        """Return whether this donor should be excluded from postal outreach."""
        return self.contact_status in self.POSTAL_SUPPRESSED_CONTACT_STATUSES
