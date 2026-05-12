"""User model for the donation management system."""

import uuid
from typing import TYPE_CHECKING

from django.contrib.auth.models import AbstractUser, Group, Permission
from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from campaigns.models import Campaign, CampaignDataFile, DataFileUpload
    from donations.models import Donation, DonationBatch
    from donors.models import DataFileDonor, Donor


class User(AbstractUser):
    """Extended user model with UUID primary key and creation tracking.

    Extends Django's AbstractUser to use UUID as primary key and track
    which user created this user account. Includes indexed username and email
    fields for efficient querying.

    Attributes:
        id: UUID primary key for the user.
        created_by: Reference to the user who created this user account.
        must_change_password: When True, forces a password change before any
            other page can be accessed. Set on admin-driven creation / reset.
        groups: Many-to-many relationship with Group model.
        user_permissions: Many-to-many relationship with Permission model.
        donors_created: Reverse relation to Donor model.
        created_campaigns: Reverse relation to Campaign model.
        created_batches: Reverse relation to DonationBatch model.
        created_data_files: Reverse relation to CampaignDataFile model.
        datafile_donors_created: Reverse relation to DataFileDonor model.
        datafile_uploads: Reverse relation to DataFileUpload model.
        filled_donations: Reverse relation to Donation model.
    """

    if TYPE_CHECKING:
        donors_created: RelatedManager[Donor]
        created_campaigns: RelatedManager[Campaign]
        created_batches: RelatedManager[DonationBatch]
        created_data_files: RelatedManager[CampaignDataFile]
        datafile_donors_created: RelatedManager[DataFileDonor]
        datafile_uploads: RelatedManager[DataFileUpload]
        filled_donations: RelatedManager[Donation]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )

    groups = models.ManyToManyField(Group, related_name="core_user_groups", blank=True)
    user_permissions = models.ManyToManyField(
        Permission, related_name="core_user_permissions", blank=True
    )

    email = models.EmailField(unique=True, verbose_name="email address")
    profile_image = models.ImageField(
        upload_to="profile_images/",
        null=True,
        blank=True,
        help_text="Optional profile avatar.",
    )
    must_change_password = models.BooleanField(
        default=False,
        help_text=(
            "When True, the user is redirected to the change-password screen "
            "before they can access any other page. Set automatically when an "
            "admin creates or resets the account."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["username"]),
            models.Index(fields=["email"]),
        ]
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self) -> str:
        """Return string representation of the user.

        Returns:
            str: Full name if set, otherwise username.
        """
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name if full_name else self.username
