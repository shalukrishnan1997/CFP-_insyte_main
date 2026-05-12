"""Audit and logging models for the donation management system."""

import uuid

from django.db import models


class ApprovalLog(models.Model):
    """Campaign approval workflow tracking.

    Logs approval status changes for campaigns.

    Attributes:
        id: UUID primary key.
        campaign: Campaign being approved.
        approver: User who approved/rejected.
        status: Approval status.
        comment: Optional approval comment.
        created_at: Timestamp of approval action.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey("campaigns.Campaign", on_delete=models.CASCADE)
    approver = models.ForeignKey("core.User", on_delete=models.SET_NULL, null=True)
    status = models.CharField(max_length=20)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return string representation of approval log.

        Returns:
            str: Log ID, campaign, status, and approver.
        """
        approver_name = self.approver.get_username() if self.approver else "system"
        return f"ApprovalLog {self.id} - {self.campaign.name} - {self.status} by {approver_name}"


class ExportLog(models.Model):
    """Data export request tracking.

    Logs export requests for campaign data.

    Attributes:
        id: UUID primary key.
        campaign: Campaign being exported.
        requested_by: User who requested the export.
        export_type: Type of export (CSV, Excel, etc.).
        status: Export status.
        created_at: Timestamp of export request.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey("campaigns.Campaign", on_delete=models.CASCADE)
    requested_by = models.ForeignKey("core.User", on_delete=models.SET_NULL, null=True)
    export_type = models.CharField(max_length=50)
    status = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return string representation of export log.

        Returns:
            str: Log ID, campaign, export type, status, and requester.
        """
        requester = self.requested_by.get_username() if self.requested_by else "system"
        return f"ExportLog {self.id} - {self.campaign.name} - {self.export_type} ({self.status}) requested by {requester}"


class AuditLog(models.Model):
    """Comprehensive audit logging for all CRUD operations.

    Tracks all create, read, update, and delete operations across the system
    with optimized performance for high-volume logging.

    Attributes:
        id: UUID primary key.
        user: User who performed the action.
        action: Type of operation (CREATE, UPDATE, DELETE, READ).
        model_name: Name of the model affected.
        object_id: ID of the affected object.
        object_repr: String representation of the object.
        changes: JSON field storing before/after values.
        ip_address: IP address of the user.
        user_agent: Browser user agent string.
        batch_id: Optional batch identifier for bulk operations.
        batch_size: Number of records affected in bulk operations.
        summary: Human-readable summary of the action.
        created_at: Timestamp of the action.
    """

    ACTION_CHOICES = [
        ("CREATE", "Create"),
        ("UPDATE", "Update"),
        ("DELETE", "Delete"),
        ("READ", "Read"),
        ("BULK_CREATE", "Bulk Create"),
        ("BULK_UPDATE", "Bulk Update"),
        ("BULK_DELETE", "Bulk Delete"),
        ("EXPORT", "Export"),
        ("IMPORT", "Import"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
        help_text="User who performed the action",
    )
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        db_index=True,
        help_text="Type of operation performed",
    )
    model_name = models.CharField(
        max_length=100, db_index=True, help_text="Model affected by the action"
    )
    object_id = models.CharField(
        max_length=255, blank=True, default="", help_text="ID of the affected object"
    )
    object_repr = models.CharField(
        max_length=500, blank=True, help_text="String representation of the object"
    )
    changes = models.JSONField(
        default=dict, blank=True, help_text="Before and after values for updates"
    )

    # Request metadata
    ip_address = models.GenericIPAddressField(
        null=True, blank=True, help_text="IP address of the user"
    )
    user_agent = models.TextField(blank=True, help_text="Browser user agent")

    # Bulk operation tracking
    batch_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Batch identifier for bulk operations",
    )
    batch_size = models.PositiveIntegerField(
        null=True, blank=True, help_text="Number of records affected in bulk operation"
    )

    # Human-readable summary
    summary = models.TextField(
        blank=True, help_text="Human-readable summary of the action"
    )

    created_at = models.DateTimeField(
        auto_now_add=True, db_index=True, help_text="Timestamp of the action"
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["model_name", "action", "created_at"]),
            models.Index(fields=["batch_id"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        """Return string representation of audit log.

        Returns:
            str: Summary of the audit log entry.
        """
        user_str = self.user.username if self.user else "System"
        return f"{user_str} - {self.action} - {self.model_name} - {self.created_at.strftime('%Y-%m-%d %H:%M')}"
