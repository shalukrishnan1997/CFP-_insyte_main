from django.contrib import admin

from campaigns.models import CampaignDataFile, DataFileUpload
from donors.models import DataFileDonor


@admin.register(CampaignDataFile)
class CampaignDataFileAdmin(admin.ModelAdmin):
    list_display = ["campaign", "total_donors", "created_by", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["campaign__name"]
    readonly_fields = ["total_donors", "created_at", "updated_at"]


class DataFileDonorInline(admin.TabularInline):
    model = DataFileDonor
    extra = 0
    fields = [
        "urn",
        "package_code",
        "full_name",
        "email",
        "phone",
        "postcode",
        "contact_status",
    ]
    readonly_fields = ["full_name"]
    can_delete = True


@admin.register(DataFileDonor)
class DataFileDonorAdmin(admin.ModelAdmin):
    list_display = [
        "urn",
        "package_code",
        "full_name",
        "data_file",
        "email",
        "phone",
        "postcode",
        "contact_status",
        "address_last_verified_at",
        "created_at",
    ]
    list_filter = [
        "data_file__campaign",
        "package_code",
        "contact_status",
        "no_thank_you",
        "created_at",
    ]
    search_fields = [
        "urn",
        "package_code",
        "first_name",
        "last_name",
        "email",
        "postcode",
        "data_file__campaign__name",
    ]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["data_file", "house_file_donor"]
    fieldsets = (
        ("Data File", {"fields": ("data_file", "package_code")}),
        ("Basic Information", {"fields": ("urn", "title", "first_name", "last_name")}),
        ("Contact Information", {"fields": ("email", "phone")}),
        (
            "Address",
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    "city",
                    "county",
                    "postcode",
                    "country",
                )
            },
        ),
        (
            "Additional Information",
            {
                "fields": (
                    "date_of_birth",
                    "age",
                    "gift_aid_declaration",
                    "gift_aid_date",
                )
            },
        ),
        (
            "Contact Status",
            {
                "fields": (
                    "contact_status",
                    "contact_status_reason",
                    "contact_status_changed_at",
                    "address_last_verified_at",
                )
            },
        ),
        (
            "GDPR & Consent",
            {
                "fields": (
                    "no_thank_you",
                    "opt_in_email",
                    "opt_in_sms",
                    "opt_in_phone",
                    "opt_in_post",
                )
            },
        ),
        (
            "House File Link",
            {
                "fields": ("house_file_donor",),
                "description": "Link to main house file donor if exists",
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


@admin.register(DataFileUpload)
class DataFileUploadAdmin(admin.ModelAdmin):
    list_display = [
        "data_file",
        "status",
        "total_rows",
        "successful_imports",
        "failed_imports",
        "uploaded_by",
        "created_at",
    ]
    list_filter = ["status", "created_at"]
    search_fields = ["data_file__campaign__name", "uploaded_by__username"]
    readonly_fields = [
        "created_at",
        "completed_at",
        "total_rows",
        "successful_imports",
        "failed_imports",
        "error_log",
    ]
    fieldsets = (
        (
            "Upload Information",
            {"fields": ("data_file", "file", "uploaded_by", "status")},
        ),
        (
            "Statistics",
            {
                "fields": (
                    "total_rows",
                    "successful_imports",
                    "failed_imports",
                    "error_log",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "completed_at")}),
    )
