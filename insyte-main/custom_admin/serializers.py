"""
API Serializers for all models
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from rest_framework import serializers

from audit.models import ApprovalLog, ExportLog
from campaigns.models import Campaign, CampaignDataFile, CampaignField, DataFileUpload
from clients.models import Client
from donations.models import Donation
from donors.models import DataFileDonor, Donor, DonorUpload, Segment

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """User serializer"""

    full_name = serializers.SerializerMethodField()
    groups = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "is_staff",
            "is_active",
            "date_joined",
            "groups",
        ]
        read_only_fields = ["id", "date_joined"]

    def get_full_name(self, obj: User) -> str:
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username


class UserCreateSerializer(serializers.ModelSerializer):
    """User creation serializer with password"""

    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True)
    groups = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Group.objects.all(), required=False
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "password_confirm",
            "is_staff",
            "is_active",
            "groups",
        ]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password": "Passwords do not match"})
        attrs.pop("password_confirm")
        return attrs

    def create(self, validated_data: dict[str, object]) -> User:
        groups = validated_data.pop("groups", [])
        password = validated_data.pop("password")
        user = User.objects.create(**validated_data)
        user.set_password(str(password) if password is not None else None)
        user.save()
        if groups:
            user.groups.set(groups)
        return user


class GroupSerializer(serializers.ModelSerializer):
    """Group serializer"""

    permissions = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Permission.objects.all(), required=False
    )
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = ["id", "name", "permissions", "user_count"]

    def get_user_count(self, obj: Group) -> int:
        # Prefer pre-annotated value from queryset (avoids N+1)
        return getattr(obj, "_user_count", obj.user_set.count())


class ClientSerializer(serializers.ModelSerializer):
    """Client/Charity serializer."""

    campaign_count = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id",
            "name",
            "description",
            "logo",
            "email",
            "phone",
            "website",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "document_ai_processor_id",
            "document_ai_location",
            "is_active",
            "created_at",
            "updated_at",
            "campaign_count",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_campaign_count(self, obj: Client) -> int:
        # Prefer pre-annotated value from queryset (avoids N+1)
        return getattr(obj, "_campaign_count", obj.campaigns.count())


class CampaignFieldSerializer(serializers.ModelSerializer):
    """Campaign field serializer"""

    class Meta:
        model = CampaignField
        fields = [
            "id",
            "label",
            "field_type",
            "placeholder",
            "help_text",
            "required",
            "is_default_field",
            "options",
            "order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CampaignListSerializer(serializers.ModelSerializer):
    """Campaign list serializer (minimal fields)"""

    created_by_name = serializers.SerializerMethodField()
    client_name = serializers.SerializerMethodField()
    donation_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    campaign_temperature_display = serializers.CharField(
        source="get_campaign_temperature_display", read_only=True
    )

    class Meta:
        model = Campaign
        fields = [
            "id",
            "name",
            "client",
            "client_name",
            "status",
            "campaign_temperature",
            "campaign_temperature_display",
            "start_date",
            "end_date",
            "created_at",
            "created_by_name",
            "donation_count",
            "total_amount",
        ]

    def get_created_by_name(self, obj: Campaign) -> str | None:
        if obj.created_by:
            return obj.created_by.get_full_name()
        return None

    def get_client_name(self, obj: Campaign) -> str | None:
        if obj.client:
            return obj.client.name
        return None

    def get_donation_count(self, obj: Campaign) -> int:
        # Prefer pre-annotated value from queryset (avoids N+1)
        return getattr(obj, "_donation_count", obj.donations.count())

    def get_total_amount(self, obj: Campaign) -> float:
        # Prefer pre-annotated value from queryset (avoids N+1)
        annotated = getattr(obj, "_total_amount", None)
        if annotated is not None:
            return float(annotated)
        from django.db.models import Sum

        result = obj.donations.aggregate(total=Sum("amount"))
        return float(result["total"] or 0)


class CampaignDetailSerializer(serializers.ModelSerializer):
    """Campaign detail serializer with nested fields"""

    fields = CampaignFieldSerializer(many=True, read_only=False, required=False)  # pyright: ignore[reportAssignmentType]
    created_by_name = serializers.CharField(
        source="created_by.get_full_name", read_only=True
    )
    donation_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    campaign_temperature_display = serializers.CharField(
        source="get_campaign_temperature_display", read_only=True
    )

    class Meta:
        model = Campaign
        fields = [
            "id",
            "name",
            "description",
            "client",
            "status",
            "status_display",
            "campaign_temperature",
            "campaign_temperature_display",
            "allow_multiple_submissions",
            "require_authentication",
            "appeal_code",
            "package_code",
            "appeal_type",
            "appeal_start",
            "appeal_end",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "fields",
            "donation_count",
            "total_amount",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "created_by"]

    def get_donation_count(self, obj: Campaign) -> int:
        # Prefer pre-annotated value from queryset (avoids N+1)
        return getattr(obj, "_donation_count", obj.donations.count())

    def get_total_amount(self, obj: Campaign) -> float:
        # Prefer pre-annotated value from queryset (avoids N+1)
        annotated = getattr(obj, "_total_amount", None)
        if annotated is not None:
            return float(annotated)
        from django.db.models import Sum

        result = obj.donations.aggregate(total=Sum("amount"))
        return float(result["total"] or 0)

    def create(self, validated_data: dict[str, object]) -> Campaign:
        fields_data: list[dict[str, object]] = list(validated_data.pop("fields", []))  # type: ignore[arg-type]
        campaign = Campaign.objects.create(**validated_data)

        # Create fields
        for field_data in fields_data:
            CampaignField.objects.create(campaign=campaign, **field_data)

        return campaign

    def update(self, instance: Campaign, validated_data: dict[str, object]) -> Campaign:
        fields_data: list[dict[str, object]] | None = validated_data.pop("fields", None)  # type: ignore[assignment]

        # Update campaign fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update fields if provided
        if fields_data is not None:
            # Delete existing custom fields
            instance.fields.filter(is_default_field=False).delete()

            # Create new fields
            for field_data in fields_data:
                field_id = field_data.get("id")
                if field_id:
                    # Update existing field
                    CampaignField.objects.filter(id=field_id, campaign=instance).update(
                        **field_data
                    )
                else:
                    # Create new field
                    CampaignField.objects.create(campaign=instance, **field_data)

        return instance


class DonationSerializer(serializers.ModelSerializer):
    """Donation serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)
    filled_by_name = serializers.CharField(
        source="filled_by.get_full_name", read_only=True
    )
    payment_status_display = serializers.CharField(
        source="get_payment_status_display", read_only=True
    )
    qa_status_display = serializers.CharField(
        source="get_qa_status_display", read_only=True
    )

    class Meta:
        model = Donation
        fields = [
            "id",
            "campaign",
            "campaign_title",
            "field_data",
            "amount",
            "payment_status",
            "payment_status_display",
            "qa_status",
            "qa_status_display",
            "filled_by",
            "filled_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "filled_by"]


class SegmentSerializer(serializers.ModelSerializer):
    """Segment serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)

    class Meta:
        model = Segment
        fields = ["id", "campaign", "campaign_title", "name", "created_at"]
        read_only_fields = ["id", "created_at"]


class ApprovalLogSerializer(serializers.ModelSerializer):
    """Approval log serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)
    approver_name = serializers.CharField(
        source="approver.get_full_name", read_only=True
    )

    class Meta:
        model = ApprovalLog
        fields = [
            "id",
            "campaign",
            "campaign_title",
            "approver",
            "approver_name",
            "status",
            "comment",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "approver"]


class ExportLogSerializer(serializers.ModelSerializer):
    """Export log serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.get_full_name", read_only=True
    )

    class Meta:
        model = ExportLog
        fields = [
            "id",
            "campaign",
            "campaign_title",
            "requested_by",
            "requested_by_name",
            "export_type",
            "status",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "requested_by"]


class DonorUploadSerializer(serializers.ModelSerializer):
    """Donor upload serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.get_full_name", read_only=True
    )

    class Meta:
        model = DonorUpload
        fields = [
            "id",
            "campaign",
            "campaign_title",
            "file",
            "uploaded_by",
            "uploaded_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "uploaded_by"]


class DashboardStatsSerializer(serializers.Serializer):
    """Dashboard statistics serializer"""

    total_campaigns = serializers.IntegerField()
    active_campaigns = serializers.IntegerField()
    total_donations = serializers.IntegerField()
    total_amount = serializers.FloatField()
    avg_donation = serializers.FloatField()
    pending_verifications = serializers.IntegerField()
    recent_donations = DonationSerializer(many=True, read_only=True)


class DonorSerializer(serializers.ModelSerializer):
    """House File Donor serializer"""

    full_name = serializers.ReadOnlyField()
    full_address = serializers.ReadOnlyField()

    class Meta:
        model = Donor
        fields = [
            "urn",
            "title",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "county",
            "postcode",
            "country",
            "full_address",
            "date_of_birth",
            "age",
            "verification_status",
            "consent_contact",
            "opt_in_email",
            "opt_in_sms",
            "opt_in_phone",
            "opt_in_post",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "created_by"]


class DonorSearchSerializer(serializers.Serializer):
    """Serializer for donor search request parameters."""

    urn = serializers.CharField(required=True, help_text="URN to search for")
    source = serializers.ChoiceField(  # pyright: ignore[reportAssignmentType]
        choices=["house_file", "data_file"],
        default="house_file",
        help_text="Source to search: house_file or data_file",
    )
    campaign_id = serializers.UUIDField(
        required=False, help_text="Campaign ID (required when source=data_file)"
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Validate that campaign_id is provided for data_file searches."""
        if attrs["source"] == "data_file" and not attrs.get("campaign_id"):
            raise serializers.ValidationError(
                {"campaign_id": "Campaign ID is required when searching data file"}
            )
        return attrs


class DonorSearchResultSerializer(serializers.Serializer):
    """Serializer for individual donor search result.

    Each result carries its source type and, for data-file donors,
    includes the data_file_donor_id UUID.
    """

    id = serializers.CharField(help_text="URN (house file) or UUID (data file)")
    data_file_donor_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="UUID of the DataFileDonor record (only for data_file source)",
    )
    urn = serializers.CharField()
    title = serializers.CharField(allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    full_name = serializers.CharField()
    email = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    address_line1 = serializers.CharField(allow_blank=True)
    address_line2 = serializers.CharField(allow_blank=True)
    city = serializers.CharField(allow_blank=True)
    county = serializers.CharField(allow_blank=True)
    postcode = serializers.CharField(allow_blank=True)
    country = serializers.CharField(allow_blank=True, required=False, default="")
    gift_aid_declaration = serializers.BooleanField(required=False, default=False)
    consent_contact = serializers.BooleanField()
    no_thank_you = serializers.BooleanField(required=False, default=False)
    opt_in_email = serializers.BooleanField()
    opt_in_sms = serializers.BooleanField()
    opt_in_phone = serializers.BooleanField()
    opt_in_post = serializers.BooleanField()
    verification_status = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Verification status (only for house_file source)",
    )
    source = serializers.ChoiceField(  # pyright: ignore[reportAssignmentType]
        choices=["house_file", "data_file"],
        help_text="Which source this result came from",
    )


class CampaignDataFileSerializer(serializers.ModelSerializer):
    """Campaign Data File serializer"""

    campaign_title = serializers.CharField(source="campaign.name", read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CampaignDataFile
        fields = [
            "campaign",
            "campaign_title",
            "total_donors",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["total_donors", "created_at", "updated_at", "created_by"]

    def get_created_by_name(self, obj: CampaignDataFile) -> str | None:
        if obj.created_by:
            return (
                f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
                or obj.created_by.username
            )
        return None


class DataFileDonorSerializer(serializers.ModelSerializer):
    """Data File Donor serializer"""

    full_name = serializers.ReadOnlyField()
    full_address = serializers.ReadOnlyField()
    campaign_title = serializers.CharField(
        source="data_file.campaign.name", read_only=True
    )

    class Meta:
        model = DataFileDonor
        fields = [
            "id",
            "data_file",
            "campaign_title",
            "urn",
            "title",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "county",
            "postcode",
            "country",
            "full_address",
            "date_of_birth",
            "age",
            "gift_aid_declaration",
            "consent_contact",
            "no_thank_you",
            "opt_in_email",
            "opt_in_sms",
            "opt_in_phone",
            "opt_in_post",
            "house_file_donor",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "created_by"]


class DataFileDonorBulkCreateSerializer(serializers.Serializer):
    """Serializer for bulk creating data file donors"""

    donors = serializers.ListField(
        child=DataFileDonorSerializer(), help_text="List of donors to create"
    )


class DataFileUploadSerializer(serializers.ModelSerializer):
    """Data File Upload serializer"""

    campaign_title = serializers.CharField(
        source="data_file.campaign.name", read_only=True
    )
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = DataFileUpload
        fields = [
            "id",
            "data_file",
            "campaign_title",
            "file",
            "uploaded_by",
            "uploaded_by_name",
            "total_rows",
            "successful_imports",
            "failed_imports",
            "error_log",
            "status",
            "created_at",
            "completed_at",
        ]
        read_only_fields = [
            "id",
            "total_rows",
            "successful_imports",
            "failed_imports",
            "error_log",
            "status",
            "created_at",
            "completed_at",
            "uploaded_by",
        ]

    def get_uploaded_by_name(self, obj: DataFileUpload) -> str | None:
        if obj.uploaded_by:
            return (
                f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip()
                or obj.uploaded_by.username
            )
        return None
