from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from scans.models import (
    RedactionSettings,
    ScanBatch,
    ScanPlaceholder,
    ScanUploadProgress,
)


@admin.register(ScanBatch)
class ScanBatchAdmin(admin.ModelAdmin):
    """Admin interface for scan batches."""

    list_display = [
        "batch_name",
        "campaign",
        "payment_method",
        "created_by",
        "created_at",
    ]
    list_filter = ["payment_method", "created_at"]
    search_fields = ["batch_name", "campaign__name"]
    autocomplete_fields = ["campaign", "created_by"]
    readonly_fields = ["batch_name", "source_filename", "created_at", "updated_at"]


@admin.register(ScanPlaceholder)
class ScanPlaceholderAdmin(admin.ModelAdmin):
    """Admin interface for scan placeholders."""

    list_display = ["urn", "donor_name", "batch", "is_captured", "created_at"]
    list_filter = ["is_captured", "created_at"]
    search_fields = ["urn", "donor_name", "batch__batch_name"]
    readonly_fields = ["created_at"]


@admin.register(ScanUploadProgress)
class ScanUploadProgressAdmin(admin.ModelAdmin):
    """Admin interface for scan upload progress."""

    list_display = [
        "campaign",
        "status",
        "total_uploaded",
        "total_expected",
        "last_upload_at",
        "updated_at",
        "last_error",
    ]
    list_filter = ["status"]
    search_fields = ["campaign__name", "last_error"]
    readonly_fields = ["updated_at"]


@admin.register(RedactionSettings)
class RedactionSettingsAdmin(admin.ModelAdmin):
    """Singleton admin for per-payment-method redaction policy."""

    list_display = [
        "__str__",
        "require_for_card",
        "require_for_direct_debit",
        "require_for_cheque",
        "updated_at",
    ]
    readonly_fields = ["updated_at", "updated_by"]
    fieldsets = (
        (
            "Redaction required before QA approval",
            {
                "fields": (
                    "require_for_card",
                    "require_for_direct_debit",
                    "require_for_caf",
                    "require_for_postal_order",
                    "require_for_cheque",
                    "require_for_cash",
                    "require_for_non_financial",
                ),
                "description": (
                    "Tick the payment methods for which the QA reviewer must "
                    "manually redact the scanned form before they can approve "
                    "the donation. Unticked methods can be approved without "
                    "redaction."
                ),
            },
        ),
        ("Audit", {"fields": ("updated_at", "updated_by")}),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Allow adding the row only if it doesn't exist yet."""
        return not RedactionSettings.objects.exists()

    def has_delete_permission(
        self, request: HttpRequest, obj: Any | None = None
    ) -> bool:
        """The singleton must always exist — never let an admin delete it."""
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: RedactionSettings,
        form: Any,
        change: bool,
    ) -> None:
        """Stamp the editing user onto the audit field on every save."""
        if request.user.is_authenticated:
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)
