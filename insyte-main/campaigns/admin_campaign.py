from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from audit.models import ApprovalLog, ExportLog
from campaigns.models import Campaign, CampaignField
from donors.models import Segment


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "status",
        "is_archived",
        "campaign_temperature",
        "client",
        "start_date",
        "end_date",
        "created_at",
    ]
    list_filter = [
        "status",
        "is_archived",
        "campaign_temperature",
        "client",
        "created_at",
    ]
    search_fields = ["name", "client__name", "description"]
    autocomplete_fields = ["client"]
    filter_horizontal = ["package_codes"]
    actions = ["archive_selected", "unarchive_selected"]

    # Disable hard-delete from the admin (audit 2026-05-02 §4.1). Use the
    # ``archive_selected`` action instead. PROTECT FKs would block the delete
    # for any campaign with linked rows anyway, but turning the affordance
    # off avoids confusing operators with cryptic ProtectedError pages.
    def has_delete_permission(  # type: ignore[override]
        self, request: HttpRequest, obj: Any = None
    ) -> bool:
        return False

    def get_actions(  # type: ignore[override]
        self, request: HttpRequest
    ) -> dict[str, Any]:
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Archive selected campaigns")
    def archive_selected(
        self, request: HttpRequest, queryset: QuerySet[Campaign]
    ) -> None:
        now = timezone.now()
        updated = queryset.filter(is_archived=False).update(
            is_archived=True, archived_at=now
        )
        self.message_user(
            request,
            f"Archived {updated} campaign(s).",
            level=messages.SUCCESS,
        )

    @admin.action(description="Un-archive selected campaigns")
    def unarchive_selected(
        self, request: HttpRequest, queryset: QuerySet[Campaign]
    ) -> None:
        updated = queryset.filter(is_archived=True).update(
            is_archived=False, archived_at=None
        )
        self.message_user(
            request,
            f"Un-archived {updated} campaign(s).",
            level=messages.SUCCESS,
        )


@admin.register(CampaignField)
class CampaignFieldAdmin(admin.ModelAdmin):
    list_display = ["label", "campaign", "field_type", "required", "order"]
    list_filter = ["field_type", "required"]
    search_fields = ["label", "campaign__name"]


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    list_display = ["name", "campaign", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["name", "campaign__name"]


@admin.register(ApprovalLog)
class ApprovalLogAdmin(admin.ModelAdmin):
    list_display = ["campaign", "status", "approver", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["campaign__name", "approver__username"]


@admin.register(ExportLog)
class ExportLogAdmin(admin.ModelAdmin):
    list_display = ["campaign", "export_type", "status", "requested_by", "created_at"]
    list_filter = ["export_type", "status", "created_at"]
    search_fields = ["campaign__name", "requested_by__username"]
