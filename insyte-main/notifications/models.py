"""Notification model for the donation management system."""

import uuid

from django.db import models

from core.models.base import CreateAndUpdateTimestampModel


class Notification(CreateAndUpdateTimestampModel):
    """In-app notifications for users.

    Tracks notifications sent to users for various events like
    batch rejections, approvals, and other system events.

    Attributes:
        id: UUID primary key.
        user: User who receives the notification.
        title: Short notification title.
        message: Full notification message.
        notification_type: Type of notification (info, success, warning, error).
        related_object_type: Type of related object (DonationBatch, Invoice, etc).
        related_object_id: ID of related object.
        link: Optional link to navigate to.
        is_read: Whether notification has been read.
        read_at: Timestamp when notification was read.
    """

    TYPE_INFO = "info"
    TYPE_SUCCESS = "success"
    TYPE_WARNING = "warning"
    TYPE_ERROR = "error"

    NOTIFICATION_TYPE_CHOICES = [
        (TYPE_INFO, "Info"),
        (TYPE_SUCCESS, "Success"),
        (TYPE_WARNING, "Warning"),
        (TYPE_ERROR, "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "core.User",
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="User who receives this notification",
    )
    title = models.CharField(max_length=200, help_text="Short notification title")
    message = models.TextField(help_text="Full notification message")
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPE_CHOICES,
        default=TYPE_INFO,
        db_index=True,
        help_text="Type of notification",
    )
    related_object_type = models.CharField(
        max_length=50, blank=True, default="", help_text="Model name of related object"
    )
    related_object_id = models.CharField(
        max_length=255, blank=True, default="", help_text="ID of related object"
    )
    link = models.CharField(
        max_length=500, blank=True, default="", help_text="Link to navigate to"
    )
    is_read = models.BooleanField(
        default=False, db_index=True, help_text="Whether notification has been read"
    )
    read_at = models.DateTimeField(
        null=True, blank=True, help_text="When notification was read"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "is_read", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} - {self.user.username}"
