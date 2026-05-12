from django.contrib import admin
from django.http import HttpRequest

from campaigns.models import PackageCode
from clients.models import Client, ClientPortalUser


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ["name", "email", "phone", "is_active", "created_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name", "email", "description"]
    fieldsets = (
        (
            "Basic Information",
            {"fields": ("name", "client_code", "description", "logo", "is_active")},
        ),
        # R2 folder path: ScanOutput/{client_code}/{appeal_code}/{payment_method}/
        ("Contact Information", {"fields": ("email", "phone", "website")}),
        (
            "Address",
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                )
            },
        ),
        (
            "Google Document AI",
            {
                "fields": (
                    "document_ai_processor_id",
                    "document_ai_location",
                    "form_field_mapping",
                ),
                "description": (
                    "Configure the charity's custom-trained Document AI processor. "
                    "The processor ID can be a bare ID (e.g. abc123def456) or the "
                    "full resource name (projects/\u2026/locations/\u2026/processors/\u2026). "
                    "Add form_field_mapping to map this charity's specific form labels "
                    "to canonical field names (donor_name, amount, gift_aid, etc.). "
                    "Universal NLP types (person, address, phone, email) are always "
                    "detected automatically."
                ),
            },
        ),
    )

    def has_delete_permission(
        self, request: HttpRequest, obj: Client | None = None
    ) -> bool:
        """Prevent deletion of clients. Only deactivation is allowed.

        Users cannot delete clients (charities). Instead, they must use the
        is_active field to deactivate clients. This preserves historical data
        and campaign records.

        Args:
            request: The HTTP request.
            obj: The client object being deleted (if any).

        Returns:
            bool: Always False to prevent deletion.
        """
        return False


@admin.register(PackageCode)
class PackageCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "description", "is_active", "created_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["code", "description"]


@admin.register(ClientPortalUser)
class ClientPortalUserAdmin(admin.ModelAdmin):
    """Admin interface for client portal users."""

    list_display = ["user", "client", "role", "is_primary", "is_active", "invited_at"]
    list_filter = ["is_active", "role", "invited_at"]
    search_fields = ["user__username", "user__email", "client__name"]
    autocomplete_fields = ["user", "client"]
