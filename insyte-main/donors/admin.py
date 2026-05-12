from django.contrib import admin

from donors.models import Donor, DonorUpload, SystemDonor


@admin.register(Donor)
class DonorAdmin(admin.ModelAdmin):
    list_display = [
        "urn",
        "full_name",
        "email",
        "phone",
        "postcode",
        "verification_status",
        "contact_status",
        "address_last_verified_at",
        "created_at",
    ]
    list_filter = [
        "verification_status",
        "contact_status",
        "consent_contact",
        "opt_in_email",
        "opt_in_sms",
        "created_at",
    ]
    search_fields = ["urn", "first_name", "last_name", "email", "postcode"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = (
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
        ("Verification", {"fields": ("verification_status",)}),
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
                    "consent_contact",
                    "opt_in_email",
                    "opt_in_sms",
                    "opt_in_phone",
                    "opt_in_post",
                )
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


@admin.register(DonorUpload)
class DonorUploadAdmin(admin.ModelAdmin):
    list_display = ["id", "campaign", "uploaded_by", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["campaign__name"]


@admin.register(SystemDonor)
class SystemDonorAdmin(admin.ModelAdmin):
    list_display = [
        "external_urn",
        "full_name",
        "client",
        "email",
        "postcode",
        "pending_review",
        "contact_status",
        "created_at",
    ]
    list_filter = [
        "pending_review",
        "contact_status",
        "client",
        "created_at",
    ]
    search_fields = [
        "external_urn",
        "first_name",
        "last_name",
        "email",
        "postcode",
    ]
    readonly_fields = ["created_at", "updated_at", "source_snapshot"]
    list_select_related = ("client",)
