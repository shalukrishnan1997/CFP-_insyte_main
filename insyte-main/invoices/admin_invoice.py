from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from invoices.models import Invoice, InvoiceSettings


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """Admin interface for invoices."""

    list_display = [
        "invoice_number",
        "client",
        "status",
        "total_amount",
        "issue_date",
        "due_date",
        "created_at",
    ]
    list_filter = ["status", "created_at"]
    search_fields = ["invoice_number", "client__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    autocomplete_fields = ["client", "campaign"]
    date_hierarchy = "created_at"

    def get_queryset(  # type: ignore[override]
        self, request: HttpRequest
    ) -> QuerySet[Any]:
        """Audit 2026-05-02 §5.1: avoid 1+N client/campaign FK lookups."""
        return super().get_queryset(request).select_related("client", "campaign")


@admin.register(InvoiceSettings)
class InvoiceSettingsAdmin(admin.ModelAdmin):
    """Admin interface for invoice settings (singleton)."""

    list_display = [
        "company_name",
        "default_tax_rate",
        "default_due_days",
        "price_per_donation",
    ]

    def has_add_permission(self, request: object) -> bool:
        """Allow only one instance."""
        return not InvoiceSettings.objects.exists()
