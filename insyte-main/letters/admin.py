from django.contrib import admin

from letters.models import LetterBatch, LetterTemplate


@admin.register(LetterTemplate)
class LetterTemplateAdmin(admin.ModelAdmin):
    """Admin interface for letter templates."""

    list_display = [
        "name",
        "template_type",
        "campaign",
        "is_active",
        "created_by",
        "created_at",
    ]
    list_filter = ["template_type", "is_active", "created_at"]
    search_fields = ["name", "campaign__name"]
    autocomplete_fields = ["campaign", "created_by"]


@admin.register(LetterBatch)
class LetterBatchAdmin(admin.ModelAdmin):
    """Admin interface for letter batches."""

    list_display = [
        "batch_number",
        "campaign",
        "template",
        "status",
        "total_letters",
        "generated_count",
        "failed_count",
        "progress_percent",
        "created_at",
    ]
    list_filter = ["status", "created_at"]
    search_fields = ["campaign__name"]
    readonly_fields = [
        "id",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "generated_count",
        "failed_count",
        "progress_percent",
        "file_count",
    ]
    autocomplete_fields = ["campaign", "template", "created_by"]
    date_hierarchy = "created_at"
