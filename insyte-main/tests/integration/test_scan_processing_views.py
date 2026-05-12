"""Integration tests for scan-processing dashboard and batch detail views."""

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from tests.factories import ScanBatchFactory, ScanPlaceholderFactory, UserFactory


@pytest.fixture()
def staff_client() -> tuple[Client, object]:
    """Return an authenticated staff client for scan-processing views."""
    user = UserFactory(is_staff=True, is_superuser=True)
    admin_group, _ = Group.objects.get_or_create(name="admin")
    user.groups.add(admin_group)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])

    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
class TestScanProcessingViews:
    """Verify staff visibility for warm-campaign scan exceptions."""

    def test_dashboard_filter_shows_only_batches_with_warm_exceptions(
        self,
        staff_client: tuple[Client, object],
    ) -> None:
        """Warm-exception filter should narrow the dashboard to affected batches."""
        client, _user = staff_client
        affected_batch = ScanBatchFactory(campaign__campaign_temperature="warm")
        clean_batch = ScanBatchFactory(campaign__campaign_temperature="cold")
        ScanPlaceholderFactory(
            batch=affected_batch,
            ocr_status="completed",
            ocr_data={"exception_reason": "qr_unreadable_for_warm_campaign"},
        )
        ScanPlaceholderFactory(batch=clean_batch, ocr_status="matched", ocr_data={})

        response = client.get(
            reverse("custom_admin:scan_processing_dashboard"),
            {"attention": "warm_exceptions"},
        )

        assert response.status_code == 200
        batches = list(response.context["scan_batches"])
        assert [batch.id for batch in batches] == [affected_batch.id]
        assert batches[0].warm_qr_issue_count == 1
        assert batches[0].warm_rescan_count == 0
        assert response.context["warm_attention_record_count"] == 1

    def test_dashboard_filter_shows_only_batches_with_mapping_gaps(
        self,
        staff_client: tuple[Client, object],
    ) -> None:
        """Mapping-gap filter should narrow the dashboard to batches needing field mapping."""
        client, _user = staff_client
        affected_batch = ScanBatchFactory()
        clean_batch = ScanBatchFactory()
        ScanPlaceholderFactory(
            batch=affected_batch,
            ocr_status="completed",
            ocr_data={"unmapped_entity_labels": ["Mystery Field"]},
        )
        ScanPlaceholderFactory(batch=clean_batch, ocr_status="matched", ocr_data={})

        response = client.get(
            reverse("custom_admin:scan_processing_dashboard"),
            {"attention": "mapping_gaps"},
        )

        assert response.status_code == 200
        batches = list(response.context["scan_batches"])
        assert [batch.id for batch in batches] == [affected_batch.id]
        assert batches[0].unmapped_placeholder_count == 1
        assert response.context["mapping_gap_record_count"] == 1
        assert response.context["mapping_gap_batch_count"] == 1

    def test_batch_detail_exposes_warm_exception_counts(
        self,
        staff_client: tuple[Client, object],
    ) -> None:
        """Batch detail should surface counts for each warm-campaign exception."""
        client, _user = staff_client
        batch = ScanBatchFactory(campaign__campaign_temperature="warm")
        ScanPlaceholderFactory(
            batch=batch,
            ocr_status="completed",
            ocr_data={"exception_reason": "qr_unreadable_for_warm_campaign"},
        )
        ScanPlaceholderFactory(
            batch=batch,
            ocr_status="completed",
            ocr_data={
                "exception_reason": "warm_source_miss_rescan_under_cold_campaign"
            },
        )

        response = client.get(
            reverse(
                "custom_admin:scan_batch_detail_view",
                kwargs={"scan_batch_id": batch.id},
            )
        )

        assert response.status_code == 200
        assert response.context["exception_counts"] == {
            "qr_unreadable_for_warm_campaign": 1,
            "qr_malformed_for_warm_campaign": 0,
            "qr_malformed_payload": 0,
            "warm_source_miss_rescan_under_cold_campaign": 1,
        }
        placeholders = list(response.context["placeholders"])
        assert {
            getattr(placeholder, "review_exception_reason", "")
            for placeholder in placeholders
        } == {
            "qr_unreadable_for_warm_campaign",
            "warm_source_miss_rescan_under_cold_campaign",
        }

    def test_qa_template_renders_malformed_qr_filter_tab(
        self,
        staff_client: tuple[Client, object],
    ) -> None:
        """Batch detail should render filter tab + count for malformed-QR codes."""
        client, _user = staff_client
        batch = ScanBatchFactory(campaign__campaign_temperature="warm")
        ScanPlaceholderFactory(
            batch=batch,
            ocr_status="completed",
            ocr_data={
                "exception_reason": "qr_malformed_for_warm_campaign",
                "qr_kind": "malformed",
                "qr_error": "raw='garbage'",
            },
        )
        ScanPlaceholderFactory(
            batch=batch,
            ocr_status="completed",
            ocr_data={
                "exception_reason": "qr_malformed_payload",
                "qr_kind": "malformed",
                "qr_error": "raw='garbage'",
            },
        )

        response = client.get(
            reverse(
                "custom_admin:scan_batch_detail_view",
                kwargs={"scan_batch_id": batch.id},
            )
        )

        assert response.status_code == 200
        assert response.context["exception_counts"] == {
            "qr_unreadable_for_warm_campaign": 0,
            "qr_malformed_for_warm_campaign": 1,
            "qr_malformed_payload": 1,
            "warm_source_miss_rescan_under_cold_campaign": 0,
        }
        body = response.content.decode()
        # Filter-tab labels for the new exception codes
        assert "Warm QR Malformed" in body
        assert "QR Malformed" in body
        # Status pill phrasing for the new codes (per-row callouts)
        assert (
            "Warm campaign QR sticker was detected but its payload is corrupt" in body
        )
        assert "QR sticker detected but payload is corrupt" in body
        # Filter tabs use Alpine x-data bindings — the click handlers should
        # reference the new exception-reason filter values.
        assert "statusFilter = 'qr_malformed_for_warm_campaign'" in body
        assert "statusFilter = 'qr_malformed_payload'" in body

    def test_batch_detail_summarizes_unmapped_ocr_labels(
        self,
        staff_client: tuple[Client, object],
    ) -> None:
        """Batch detail should aggregate unmapped OCR labels for onboarding review."""
        client, _user = staff_client
        batch = ScanBatchFactory()
        ScanPlaceholderFactory(
            batch=batch,
            ocr_data={
                "unmapped_entity_labels": [
                    "Supporter Favourite Colour",
                    "Mystery Field",
                ]
            },
        )
        ScanPlaceholderFactory(
            batch=batch,
            ocr_data={"unmapped_entity_labels": ["Mystery Field"]},
        )
        ScanPlaceholderFactory(batch=batch, ocr_data={})

        response = client.get(
            reverse(
                "custom_admin:scan_batch_detail_view",
                kwargs={"scan_batch_id": batch.id},
            )
        )

        assert response.status_code == 200
        assert response.context["unmapped_placeholder_count"] == 2
        assert response.context["unmapped_label_summary"] == [
            {"label": "Mystery Field", "count": 2},
            {"label": "Supporter Favourite Colour", "count": 1},
        ]
        assert (
            response.context["mapping_draft_placeholder"]
            == "__map_to_canonical_field__"
        )
        assert response.context["mapping_draft_json"] == (
            "{\n"
            '  "Mystery Field": "__map_to_canonical_field__",\n'
            '  "Supporter Favourite Colour": "__map_to_canonical_field__"\n'
            "}"
        )
        assert "Unmapped OCR labels in this batch" in response.content.decode()
        assert "Draft form_field_mapping JSON" in response.content.decode()
