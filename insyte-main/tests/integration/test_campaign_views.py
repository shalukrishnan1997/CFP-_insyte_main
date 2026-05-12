"""Integration tests for Campaign views.

FIN-CAMP-INT-* test cases covering page loads, form submissions,
authentication, and validation via HTTP client.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from campaigns.models import Campaign, CampaignField
from tests.factories import CampaignFactory, ClientFactory, UserFactory


@pytest.fixture()
def staff_client() -> tuple[Client, object]:
    """Return authenticated client and staff user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


def _seed_campaign_edit_fields(campaign: Campaign) -> CampaignField:
    """Create one default and two custom fields for edit-flow tests."""
    CampaignField.objects.create(
        campaign=campaign,
        label="Donor Name",
        field_type=CampaignField.FIELD_TEXT,
        is_default_field=True,
        order=1,
    )
    editable_field = CampaignField.objects.create(
        campaign=campaign,
        label="Legacy Reference",
        field_type=CampaignField.FIELD_TEXT,
        required=False,
        order=2,
    )
    CampaignField.objects.create(
        campaign=campaign,
        label="Old Preference",
        field_type=CampaignField.FIELD_DROPDOWN,
        options=["Email", "Phone"],
        order=3,
    )
    return editable_field


def _build_campaign_edit_payload(
    campaign: Campaign,
    linked_client_id: str,
    existing_custom_field_id: str,
) -> dict[str, str]:
    """Build a campaign edit POST payload including custom field updates."""
    return {
        "title": campaign.name,
        "description": campaign.description,
        "client_id": linked_client_id,
        "appeal_code": campaign.appeal_code,
        "appeal_type": campaign.appeal_type,
        "appeal_start": "20/03/2026",
        "appeal_end": "20/04/2026",
        "package_codes": "PKX1,PKX2",
        "campaign_temperature": "cold",
        "donor_source": "house_file",
        "scan_purpose": "donation",
        "campaign_manager_emails": "manager@example.org",
        "status": campaign.status,
        "extra_fields_json": json.dumps(
            [
                {
                    "id": existing_custom_field_id,
                    "label": "Legacy Reference Updated",
                    "field_type": CampaignField.FIELD_TEXTAREA,
                    "required": True,
                    "options": [],
                },
                {
                    "label": "Contact Preference",
                    "field_type": CampaignField.FIELD_RADIO,
                    "required": False,
                    "options": ["Email", "Phone"],
                },
            ]
        ),
    }


def _assert_updated_custom_fields(campaign: Campaign) -> None:
    """Assert updated campaign field state after an edit submission."""
    non_default_fields = list(
        campaign.fields.filter(is_default_field=False).order_by("order")
    )
    assert set(campaign.package_codes.values_list("code", flat=True)) == {
        "PKX1",
        "PKX2",
    }
    assert campaign.fields.filter(is_default_field=True, label="Donor Name").exists()
    assert [field.label for field in non_default_fields] == [
        "Legacy Reference Updated",
        "Contact Preference",
    ]
    assert non_default_fields[0].field_type == CampaignField.FIELD_TEXTAREA
    assert non_default_fields[0].required is True
    assert non_default_fields[1].field_type == CampaignField.FIELD_RADIO
    assert non_default_fields[1].options == ["Email", "Phone"]


# ═══════════════════════════════════════════════════════════════
# Campaign Views — Page Loads
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignViewsLoad:
    """FIN-CAMP-INT-001 to 003: Campaign pages load correctly."""

    def test_campaign_list_redirects_to_default_active_filter(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Campaign list redirects to the default active filter when no params exist."""
        client, _ = staff_client

        response = client.get(reverse("custom_admin:admin_campaigns"))

        assert response.status_code == 302
        assert response.url.endswith("/admin/campaigns/?status=active")

    def test_campaign_list_page_loads(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """FIN-CAMP-INT-001: Campaign list returns 200 OK."""
        client, _ = staff_client
        CampaignFactory()
        response = client.get(reverse("custom_admin:admin_campaigns"), follow=True)
        assert response.status_code == 200

    def test_campaign_create_page_loads(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """FIN-CAMP-INT-002: Campaign create form returns 200."""
        client, _ = staff_client
        response = client.get(reverse("custom_admin:campaign_create"))
        assert response.status_code == 200

    def test_campaign_detail_page_loads(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """FIN-CAMP-INT-003: Campaign detail page loads."""
        client, _ = staff_client
        campaign = CampaignFactory()
        response = client.get(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            )
        )
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════
# Campaign Views — Authentication
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignViewsAuth:
    """FIN-CAMP-INT-004 to 005: Authentication requirements."""

    def test_unauthenticated_user_redirected(self) -> None:
        """FIN-CAMP-INT-004: Anonymous user redirected to login."""
        client = Client()
        response = client.get(reverse("custom_admin:admin_campaigns"))
        assert response.status_code in [302, 301]

    def test_non_staff_user_denied(self) -> None:
        """FIN-CAMP-INT-005: Non-staff user gets 403 or redirect."""
        user = UserFactory(is_staff=False)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("custom_admin:admin_campaigns"))
        assert response.status_code in [302, 403]


# ═══════════════════════════════════════════════════════════════
# Campaign Views — CRUD Operations
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestCampaignViewsCRUD:
    """FIN-CAMP-INT-006 to 008: Create and edit campaign forms."""

    def test_campaign_edit_loads(self, staff_client: tuple[Client, object]) -> None:
        """FIN-CAMP-INT-006: Campaign edit page loads with data."""
        client, _ = staff_client
        campaign = CampaignFactory(name="EditMe")
        response = client.get(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            )
        )
        assert response.status_code == 200
        body = response.content.decode()
        assert reverse("custom_admin:campaign-data-file-list") in body

    def test_campaign_edit_links_to_donor_imports_for_data_file(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Campaign edit exposes Manage Import (donor imports) for data-file mode."""
        client, _ = staff_client
        campaign = CampaignFactory(name="EditMe")

        response = client.get(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            )
        )

        assert response.status_code == 200
        body = response.content.decode()
        imports_url = reverse("custom_admin:donor_imports")
        assert imports_url in body
        assert f"campaign_id={campaign.pk}" in body
        assert f"client_id={campaign.client_id}" in body

    def test_campaign_import_sample_downloads_csv(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """The sample import download endpoint returns the CSV payload."""
        client, _ = staff_client

        response = client.get(reverse("custom_admin:campaign_import_sample"))

        assert response.status_code == 200
        assert response["Content-Disposition"] == (
            'attachment; filename="pipe_delimited_import_sample.csv"'
        )
        assert response["Content-Type"].startswith("application/octet-stream")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"urn|title|first_name|last_name" in b"".join(response.streaming_content)

    def test_campaign_import_sample_downloads_for_staff_without_campaign_permission(
        self,
    ) -> None:
        """Staff users can download the sample from the donor-import screen."""
        user = UserFactory(is_staff=True, is_superuser=False)
        client = Client()
        client.force_login(user)

        response = client.get(reverse("custom_admin:campaign_import_sample"))

        assert response.status_code == 200
        assert response["Content-Disposition"] == (
            'attachment; filename="pipe_delimited_import_sample.csv"'
        )
        assert response["Content-Type"].startswith("application/octet-stream")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"urn|title|first_name|last_name" in b"".join(response.streaming_content)

    def test_campaign_import_sample_falls_back_when_static_file_missing(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """The sample download still works when the bundled CSV is missing."""
        client, _ = staff_client

        with patch.object(Path, "is_file", return_value=False):
            response = client.get(reverse("custom_admin:campaign_import_sample"))

        assert response.status_code == 200
        assert response["Content-Disposition"] == (
            'attachment; filename="pipe_delimited_import_sample.csv"'
        )
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"urn|title|first_name|last_name" in b"".join(response.streaming_content)

    def test_campaign_list_shows_campaigns(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """FIN-CAMP-INT-007: Campaign list page contains campaign names."""
        client, _ = staff_client
        CampaignFactory(name="Winter Relief 2026", status="active")
        CampaignFactory(name="Spring Appeal 2026", status="active")
        response = client.get(reverse("custom_admin:admin_campaigns"), follow=True)
        content = response.content.decode()
        assert "Winter Relief 2026" in content
        assert "Spring Appeal 2026" in content

    def test_campaign_detail_shows_correct_data(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """FIN-CAMP-INT-008: Detail page shows matching campaign data."""
        client, _ = staff_client
        campaign = CampaignFactory(
            name="Detail Test Campaign",
            description="Testing detail page",
            status="active",
        )
        response = client.get(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            )
        )
        content = response.content.decode()
        assert "Detail Test Campaign" in content

    def test_campaign_create_validation_preserves_posted_values(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Invalid create submissions re-render the form with posted values."""
        client, _ = staff_client
        linked_client = ClientFactory(name="North Charity")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            {
                "title": "",
                "client_id": str(linked_client.id),
                "appeal_code": "NORTH26",
                "appeal_start": "20/03/2026",
                "appeal_end": "20/04/2026",
                "campaign_temperature": "warm",
                "donor_source": "data_file",
                "scan_purpose": "donation",
            },
        )

        assert response.status_code == 200
        assert response.context["form_data"]["client_id"] == str(linked_client.id)
        assert response.context["form_data"]["appeal_code"] == "NORTH26"
        assert response.context["form_data"]["donor_source"] == "data_file"

    def test_campaign_create_links_package_codes_and_default_fields(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Valid create submissions persist package codes and default campaign fields."""
        client, staff_user = staff_client
        linked_client = ClientFactory(name="South Charity")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            {
                "title": "Spring Campaign",
                "description": "Seasonal appeal",
                "client_id": str(linked_client.id),
                "appeal_code": "SPRING26",
                "appeal_type": "Donation",
                "appeal_start": "20/03/2026",
                "appeal_end": "20/04/2026",
                "campaign_temperature": "cold",
                "donor_source": "house_file",
                "scan_purpose": "donation",
                "package_codes": "pk1, pk2",
            },
        )

        assert response.status_code == 302
        created_campaign = Campaign.objects.get(
            name="Spring Campaign",
            client=linked_client,
        )
        assert created_campaign.created_by == staff_user
        assert set(created_campaign.package_codes.values_list("code", flat=True)) == {
            "PK1",
            "PK2",
        }
        assert CampaignField.objects.filter(campaign=created_campaign).count() == 6

    def test_campaign_edit_shows_existing_custom_fields(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Edit page exposes the configured campaign custom fields."""
        client, _ = staff_client
        campaign = CampaignFactory(name="Editable Campaign")
        _seed_campaign_edit_fields(campaign)
        CampaignField.objects.create(
            campaign=campaign,
            label="Gift Aid Reference",
            field_type=CampaignField.FIELD_TEXT,
            required=True,
            order=4,
        )

        response = client.get(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            )
        )

        assert response.status_code == 200
        assert "Custom Fields" in response.content.decode()
        assert "Gift Aid Reference" in response.content.decode()

    def test_campaign_edit_updates_custom_fields(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Editing a campaign can add, update, and remove custom fields."""
        client, _ = staff_client
        linked_client = ClientFactory(name="West Charity")
        campaign = CampaignFactory(
            name="Editable Campaign",
            client=linked_client,
            appeal_code="EDIT26",
            appeal_type="Donation",
        )
        existing_custom_field = _seed_campaign_edit_fields(campaign)

        response = client.post(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            ),
            _build_campaign_edit_payload(
                campaign,
                str(linked_client.id),
                str(existing_custom_field.id),
            ),
        )

        assert response.status_code == 302

        campaign.refresh_from_db()
        _assert_updated_custom_fields(campaign)


# ═══════════════════════════════════════════════════════════════
# Campaign Views — Appeal Code Uniqueness
# ═══════════════════════════════════════════════════════════════


def _appeal_code_create_payload(
    *, title: str, client_id: str, appeal_code: str
) -> dict[str, str]:
    """Build a minimal valid create-campaign POST payload."""
    return {
        "title": title,
        "description": "",
        "client_id": client_id,
        "appeal_code": appeal_code,
        "appeal_type": "Donation",
        "appeal_start": "20/03/2026",
        "appeal_end": "20/04/2026",
        "campaign_temperature": "cold",
        "donor_source": "house_file",
        "scan_purpose": "donation",
    }


@pytest.mark.django_db()
class TestCampaignAppealCodeUniqueness:
    """Appeal codes must be unique per client and case-insensitive."""

    def test_create_rejects_duplicate_appeal_code_for_same_client(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """A second campaign for the same client cannot reuse an appeal code."""
        client, _ = staff_client
        linked_client = ClientFactory(name="Charity A")
        CampaignFactory(client=linked_client, appeal_code="WINT25")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            _appeal_code_create_payload(
                title="Second Winter Appeal",
                client_id=str(linked_client.id),
                appeal_code="WINT25",
            ),
        )

        assert response.status_code == 200
        assert (
            Campaign.objects.filter(client=linked_client, appeal_code="WINT25").count()
            == 1
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("appeal code 'WINT25'" in msg for msg in messages)

    def test_create_allows_same_appeal_code_for_different_client(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Different clients may both use the same appeal code."""
        client, _ = staff_client
        client_a = ClientFactory(name="Charity A")
        client_b = ClientFactory(name="Charity B")
        CampaignFactory(client=client_a, appeal_code="WINT25")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            _appeal_code_create_payload(
                title="Charity B Winter",
                client_id=str(client_b.id),
                appeal_code="WINT25",
            ),
        )

        assert response.status_code == 302
        assert Campaign.objects.filter(client=client_b, appeal_code="WINT25").exists()

    def test_create_appeal_code_is_case_insensitive(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Appeal codes are normalized to uppercase, so case mismatches collide."""
        client, _ = staff_client
        linked_client = ClientFactory(name="Charity A")
        CampaignFactory(client=linked_client, appeal_code="WINT25")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            _appeal_code_create_payload(
                title="Lowercase Duplicate",
                client_id=str(linked_client.id),
                appeal_code="wint25",
            ),
        )

        assert response.status_code == 200
        assert (
            Campaign.objects.filter(client=linked_client, appeal_code="WINT25").count()
            == 1
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("appeal code 'WINT25'" in msg for msg in messages)

    def test_create_collides_with_legacy_lowercase_appeal_code(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Legacy rows stored lowercase must still be caught by the duplicate check."""
        client, _ = staff_client
        linked_client = ClientFactory(name="Charity A")
        # Bypass factory normalization to simulate pre-normalization legacy data.
        Campaign.objects.filter(
            pk=CampaignFactory(client=linked_client, appeal_code="WINT25").pk
        ).update(appeal_code="spring25")

        response = client.post(
            reverse("custom_admin:campaign_create"),
            _appeal_code_create_payload(
                title="Uppercase Collide Legacy",
                client_id=str(linked_client.id),
                appeal_code="SPRING25",
            ),
        )

        assert response.status_code == 200
        assert (
            Campaign.objects.filter(
                client=linked_client, name="Uppercase Collide Legacy"
            ).count()
            == 0
        )
        messages = [str(m) for m in response.context["messages"]]
        assert any("appeal code 'SPRING25'" in msg for msg in messages)

    def test_edit_does_not_flag_self_as_duplicate(
        self, staff_client: tuple[Client, object]
    ) -> None:
        """Editing an existing campaign without changing the code is allowed."""
        client, _ = staff_client
        linked_client = ClientFactory(name="Charity A")
        campaign = CampaignFactory(
            client=linked_client, appeal_code="WINT25", name="Winter Appeal"
        )

        response = client.post(
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.pk},
            ),
            {
                "title": "Winter Appeal",
                "description": "",
                "client_id": str(linked_client.id),
                "appeal_code": "WINT25",
                "appeal_type": "Donation",
                "appeal_start": "20/03/2026",
                "appeal_end": "20/04/2026",
                "package_codes": "",
                "campaign_temperature": "cold",
                "donor_source": "house_file",
                "scan_purpose": "donation",
                "campaign_manager_emails": "",
                "status": campaign.status,
                "extra_fields_json": "[]",
            },
        )

        assert response.status_code == 302
        campaign.refresh_from_db()
        assert campaign.appeal_code == "WINT25"
