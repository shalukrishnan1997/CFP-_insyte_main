from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from donations.models import Donation


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    """Admin interface for individual donations."""

    list_display = [
        "id",
        "campaign",
        "donor",
        "amount",
        "currency",
        "payment_method",
        "qa_status",
        "payment_status",
        "donation_date",
        "created_at",
    ]

    def get_queryset(  # type: ignore[override]
        self, request: HttpRequest
    ) -> QuerySet[Any]:
        """Audit 2026-05-02 §5.1: avoid 1+N FK lookups on the change-list."""
        return (
            super()
            .get_queryset(request)
            .select_related("campaign", "donor", "data_file_donor", "batch")
        )

    list_filter = [
        "payment_method",
        "qa_status",
        "payment_status",
        "currency",
        "gift_aid",
        "created_at",
    ]
    search_fields = [
        "campaign__name",
        "donor__first_name",
        "donor__last_name",
        "donor__urn",
    ]
    readonly_fields = ["id", "created_at", "updated_at"]
    autocomplete_fields = [
        "campaign",
        "batch",
        "donor",
        "data_file_donor",
        "paying_in_slip",
    ]
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Donation Info",
            {
                "fields": (
                    "id",
                    "campaign",
                    "batch",
                    "donor_source",
                    "amount",
                    "currency",
                    "donation_date",
                ),
            },
        ),
        (
            "Donor",
            {
                "fields": ("donor", "data_file_donor"),
            },
        ),
        (
            "Payment",
            {
                "fields": ("payment_method", "payment_status", "paying_in_slip"),
            },
        ),
        (
            "Card Details",
            {
                "fields": ("card_holder_name", "card_last_four", "card_expiry_date"),
                "classes": ("collapse",),
            },
        ),
        (
            "QA",
            {
                "fields": ("qa_status", "qa_notes"),
            },
        ),
        (
            "Gift Aid & Letters",
            {
                "fields": ("gift_aid", "letter_status", "letter_batch"),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("filled_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
