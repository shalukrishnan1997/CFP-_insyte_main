"""Django admin registration for audit log models."""

from django.contrib import admin

from audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Admin interface for audit logs (read-only)."""

    list_display = ["user", "action", "model_name", "object_repr", "created_at"]
    list_filter = ["action", "model_name", "created_at"]
    search_fields = ["user__username", "model_name", "object_repr", "summary"]
    readonly_fields = [
        "id",
        "user",
        "action",
        "model_name",
        "object_id",
        "object_repr",
        "changes",
        "ip_address",
        "user_agent",
        "batch_id",
        "batch_size",
        "summary",
        "created_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: object) -> bool:
        """Audit logs should never be manually created."""
        return False

    def has_change_permission(self, request: object, obj: object = None) -> bool:
        """Audit logs should never be edited."""
        return False

    def has_delete_permission(self, request: object, obj: object = None) -> bool:
        """Audit logs should never be deleted from admin."""
        return False
