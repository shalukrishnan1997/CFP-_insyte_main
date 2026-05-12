from django.contrib import admin

from invoices.models import ServiceCategory, ServiceItem


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    """Admin interface for service categories."""

    list_display = ["name", "is_active", "order", "item_count", "created_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name", "description"]
    ordering = ["order", "name"]

    def item_count(self, obj: ServiceCategory) -> int:
        """Display count of service items in this category."""
        return obj.service_items.count()

    item_count.short_description = "Items"  # type: ignore[attr-defined]


@admin.register(ServiceItem)
class ServiceItemAdmin(admin.ModelAdmin):
    """Admin interface for service items."""

    list_display = [
        "description",
        "category",
        "unit_price",
        "pricing_unit",
        "is_active",
        "is_default",
    ]
    list_filter = ["is_active", "is_default", "category", "created_at"]
    search_fields = ["description", "notes", "pricing_unit"]
    list_editable = ["unit_price", "is_active", "is_default"]
    ordering = ["category__order", "order", "description"]

    fieldsets = (
        ("Service Information", {"fields": ("category", "description", "notes")}),
        ("Pricing", {"fields": ("unit_price", "pricing_unit")}),
        ("Settings", {"fields": ("is_active", "is_default", "order")}),
    )
