from django.contrib import admin

from banking.models import PayingInSlip


@admin.register(PayingInSlip)
class PayingInSlipAdmin(admin.ModelAdmin):
    """Admin interface for paying-in slips."""

    list_display = [
        "slip_number",
        "client",
        "payment_type",
        "banking_date",
        "total_amount",
        "total_items",
        "status",
        "created_at",
    ]
    list_filter = ["status", "payment_type", "banking_date", "completion_status"]
    search_fields = ["slip_number", "client__name"]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["client", "created_by", "banked_by", "processed_by"]
    date_hierarchy = "banking_date"

    fieldsets = (
        (
            "Slip Info",
            {
                "fields": ("slip_number", "client", "payment_type", "banking_date"),
            },
        ),
        (
            "Totals",
            {
                "fields": ("total_amount", "total_items"),
            },
        ),
        (
            "Status",
            {
                "fields": ("status", "notes"),
            },
        ),
        (
            "Banking",
            {
                "fields": ("banked_at", "banked_by"),
                "classes": ("collapse",),
            },
        ),
        (
            "Processing",
            {
                "fields": (
                    "bank_processed_date",
                    "processed_amount",
                    "completion_status",
                    "processing_issues",
                    "custom_issue",
                    "processed_by",
                    "processed_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
