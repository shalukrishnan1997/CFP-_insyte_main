"""Client models for the donation management system."""

import uuid
from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from campaigns.models import Campaign


class Client(models.Model):
    """Client or charity organization model.

    Represents a charity or client organization that runs donation campaigns.
    Includes contact information, address details, and logo.

    Attributes:
        id: UUID primary key.
        name: Unique name of the client/charity.
        description: About the charity (optional).
        logo: Client logo image (optional).
        email: Primary contact email.
        phone: Contact phone number.
        website: Client website URL.
        address_line1: First line of address.
        address_line2: Second line of address.
        city: City name.
        state: State/province name.
        postal_code: Postal/ZIP code.
        country: Country name.
        document_ai_processor_id: Full resource name of the charity's custom
            Document AI processor (e.g. projects/…/processors/…).
        document_ai_location: GCP region for the processor (default "eu").
        is_active: Whether the client is active.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        campaigns: Reverse relation to Campaign model.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255, unique=True, help_text="Client/Charity name"
    )
    client_code = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Short uppercase code for this client (e.g. 'BRC' for British Red Cross). "
            "Used as the first folder segment in R2 scan paths: "
            "ScanOutput/{client_code}/{appeal_code}/{payment_method}/. "
            "Use uppercase letters/digits only, no spaces."
        ),
    )
    description = models.TextField(blank=True, help_text="About the charity")
    logo = models.ImageField(
        upload_to="uploads/client_logos/",
        blank=True,
        null=True,
        help_text="Client logo",
    )

    # Contact information
    email = models.EmailField(blank=True, help_text="Primary contact email")
    phone = models.CharField(
        max_length=20, blank=True, help_text="Contact phone number"
    )
    website = models.URLField(blank=True, help_text="Client website URL")

    # Address
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True, default="")

    # ─── Google Document AI Configuration ────────────────────────────────────
    # Each charity has its own custom-trained Document AI extractor processor.
    # Set via the admin UI.  Leave blank if not yet configured.
    document_ai_processor_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Google Document AI processor ID for this charity's donation forms. "
            "Format: projects/{project}/locations/{location}/processors/{id}. "
            "Leave blank if Document AI is not yet configured for this client."
        ),
    )
    document_ai_location = models.CharField(
        max_length=20,
        blank=True,
        default="eu",
        help_text=(
            "Document AI processor location (e.g. 'eu' or 'us'). "
            "Must match the region where the processor was created."
        ),
    )
    form_field_mapping = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "JSON mapping of this charity's form field labels to canonical "
            "extraction field names.  Keys are the verbatim label text from the "
            "donation form (lowercased, spaces OK).  Values are canonical field "
            "names such as donor_name, donor_first_name, donor_last_name, amount, "
            "gift_aid, address_line1, address_line2, city, postcode, phone, "
            "email, address_block, contact_consent, email_consent, sms_consent, "
            "phone_consent, post_consent.  \n\n"
            "Example (raffle form):\n"
            '{"full name": "donor_name", "i would like to purchase \u00a3": "amount", '
            '"i am a uk taxpayer": "gift_aid"}\n\n'
            "Universal NLP types (person, address, phone, email) are detected "
            "automatically and do NOT need to be listed here."
        ),
    )

    # Metadata
    is_active = models.BooleanField(default=True, help_text="Is this client active?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        campaigns: RelatedManager[Campaign]
        portal_users: RelatedManager[ClientPortalUser]

    class Meta:
        ordering = ["name"]
        verbose_name = "clients.Client"
        verbose_name_plural = "Clients"

    def __str__(self) -> str:
        """Return string representation of the client.

        Returns:
            str: The client's name.
        """
        return self.name


class ClientPortalUser(models.Model):
    """Client portal user model for multiple users per client.

    Allows multiple users to access the same client's portal data.
    Each user has their own login credentials and can have different
    roles and permissions.

    Attributes:
        id: UUID primary key.
        client: The client this user belongs to.
        user: The Django user account.
        role: User role (admin, viewer, etc).
        is_primary: Whether this is the primary contact.
        invited_by: Staff user who created this portal user.
        invited_at: Timestamp when user was created.
        last_login: Last login timestamp.
        is_active: Whether this portal user is active.
    """

    ROLE_CHOICES = [
        ("admin", "Administrator"),
        ("viewer", "Viewer"),
        ("manager", "Manager"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="portal_users",
        help_text="Client organization this user belongs to",
    )
    user = models.OneToOneField(
        "core.User",
        on_delete=models.CASCADE,
        related_name="client_portal_profile",
        help_text="Django user account",
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="viewer",
        help_text="User role within the client portal",
    )
    is_primary = models.BooleanField(
        default=False, help_text="Primary contact for this client"
    )
    invited_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invited_portal_users",
        help_text="Staff user who created this portal user",
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Is this portal user active?"
    )

    class Meta:
        ordering = ["-is_primary", "user__first_name", "user__last_name"]
        verbose_name = "Client Portal User"
        verbose_name_plural = "Client Portal Users"
        indexes = [
            models.Index(fields=["client", "is_active"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self) -> str:
        """Return string representation of the portal user.

        Returns:
            str: User's full name and client name.
        """
        return f"{self.user.get_full_name() or self.user.username} ({self.client.name})"
