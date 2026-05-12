from django.contrib import admin
from django.http import HttpRequest

from donations.models import DonationBatch


@admin.register(DonationBatch)
class DonationBatchAdmin(admin.ModelAdmin):
    """Admin interface for donation batches."""

    list_display = [
        "batch_name",
        "campaign",
        "status",
        "payment_status",
        "total_donations",
        "total_amount",
        "created_by",
        "created_at",
    ]
    list_filter = ["status", "payment_status", "default_currency", "created_at"]
    search_fields = ["batch_name", "campaign__name", "created_by__username"]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["campaign", "created_by", "reviewed_by"]
    date_hierarchy = "created_at"

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: DonationBatch | None = None,
    ) -> list[str] | tuple[str, ...]:
        """Protect physical batch identifiers after creation."""
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and "batch_name" not in fields:
            fields.append("batch_name")
        return fields

    fieldsets = (
        (
            "Batch Info",
            {
                "fields": ("batch_name", "campaign", "status", "created_by"),
            },
        ),
        (
            "Payment Defaults",
            {
                "fields": ("default_payment_method", "default_currency"),
            },
        ),
        (
            "Totals",
            {
                "fields": ("total_donations", "total_amount"),
            },
        ),
        (
            "Payment Processing",
            {
                "fields": (
                    "payment_status",
                    "payment_initiated_at",
                    "payment_completed_at",
                    "payment_initiated_by",
                    "successful_payment_count",
                    "failed_payment_count",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "QA Review",
            {
                "fields": ("reviewed_by", "reviewed_at", "review_notes"),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
