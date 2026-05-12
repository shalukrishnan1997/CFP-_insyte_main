"""Additional tests for core/scan_tasks.py — helper utilities and housekeeping tasks."""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from tests.factories import (
    CampaignFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

# ──────────────────────────────────────────────────────────────────────────────
# Pure helper functions
# ──────────────────────────────────────────────────────────────────────────────


class TestFilterImageKeys:
    """Tests for _filter_image_keys helper."""

    def test_filters_to_image_extensions(self) -> None:
        from scans.tasks import _filter_image_keys

        keys = ["scan.jpg", "doc.pdf", "data.csv", "photo.png", "report.txt"]
        result = _filter_image_keys(keys)
        assert sorted(result) == sorted(["scan.jpg", "doc.pdf", "photo.png"])

    def test_empty_list_returns_empty(self) -> None:
        from scans.tasks import _filter_image_keys

        assert _filter_image_keys([]) == []

    def test_case_insensitive_extension_matching(self) -> None:
        from scans.tasks import _filter_image_keys

        keys = ["SCAN.JPG", "scan.TIFF", "image.BMP", "file.docx"]
        result = _filter_image_keys(keys)
        assert len(result) == 3

    def test_all_non_image_returns_empty(self) -> None:
        from scans.tasks import _filter_image_keys

        keys = ["data.csv", "report.xlsx", "doc.docx"]
        assert _filter_image_keys(keys) == []


@pytest.mark.django_db()
class TestResolveUser:
    """Tests for _resolve_user helper."""

    def test_none_user_id_returns_none(self) -> None:
        from scans.tasks import _resolve_user

        assert _resolve_user(None) is None

    def test_zero_user_id_returns_none(self) -> None:
        from scans.tasks import _resolve_user

        assert _resolve_user(0) is None

    def test_valid_user_id_returns_user(self) -> None:
        from scans.tasks import _resolve_user

        user = UserFactory()
        result = _resolve_user(user.pk)
        assert result is not None
        assert result.pk == user.pk  # type: ignore[union-attr]

    def test_nonexistent_user_id_returns_none(self) -> None:
        from scans.tasks import _resolve_user

        assert _resolve_user(999999999) is None


class TestAggregateScansResults:
    """Tests for _aggregate_scan_results helper."""

    def test_empty_results_returns_zeros(self) -> None:
        from scans.tasks import _aggregate_scan_results

        total, matched, failed = _aggregate_scan_results([])
        assert total == 0
        assert matched == 0
        assert failed == 0

    def test_counts_matched_correctly(self) -> None:
        from scans.models import ScanPlaceholder
        from scans.tasks import _aggregate_scan_results

        results = [
            {"ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED},
            {"ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED},
            {"ocr_status": ScanPlaceholder.OCR_STATUS_FAILED},
        ]
        total, matched, failed = _aggregate_scan_results(results)
        assert total == 3
        assert matched == 2
        assert failed == 1

    def test_non_dict_entries_count_as_failed(self) -> None:
        from scans.tasks import _aggregate_scan_results

        results = [
            {"ocr_status": "matched"},
            Exception("crash"),  # type: ignore[list-item]
            None,  # type: ignore[list-item]
        ]
        total, _matched, failed = _aggregate_scan_results(results)
        assert total == 3
        assert failed == 2  # Exception and None are non-dict


# ──────────────────────────────────────────────────────────────────────────────
# Housekeeping tasks
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestCleanupStaleScanProgressTask:
    """Tests for cleanup_stale_scan_progress_task."""

    def test_no_stale_records_returns_zero(self) -> None:
        from scans.tasks import cleanup_stale_scan_progress_task

        result = cleanup_stale_scan_progress_task()
        assert result == {"status": "ok", "cleaned_up": 0}

    def test_stale_scanning_record_is_reset(self) -> None:
        from scans.models import ScanUploadProgress
        from scans.tasks import cleanup_stale_scan_progress_task

        campaign = CampaignFactory()
        # Create a scanning record with old last_upload_at
        old_time = timezone.now() - timedelta(hours=3)
        ScanUploadProgress.objects.create(
            campaign=campaign,
            status=ScanUploadProgress.STATUS_SCANNING,
            last_upload_at=old_time,
        )

        result = cleanup_stale_scan_progress_task()
        assert result["cleaned_up"] == 1

        record = ScanUploadProgress.objects.get(campaign=campaign)
        assert record.status == ScanUploadProgress.STATUS_ERROR

    def test_recent_scanning_record_is_not_reset(self) -> None:
        from scans.models import ScanUploadProgress
        from scans.tasks import cleanup_stale_scan_progress_task

        campaign = CampaignFactory()
        # Recent scanning record (within 2 hours)
        ScanUploadProgress.objects.create(
            campaign=campaign,
            status=ScanUploadProgress.STATUS_SCANNING,
            last_upload_at=timezone.now() - timedelta(minutes=30),
        )

        result = cleanup_stale_scan_progress_task()
        assert result["cleaned_up"] == 0


@pytest.mark.django_db()
class TestResetStuckScanBatchesTask:
    """Tests for reset_stuck_scan_batches_task."""

    def test_no_stuck_batches_returns_zero(self) -> None:
        from scans.tasks import reset_stuck_scan_batches_task

        result = reset_stuck_scan_batches_task()
        assert result == {"status": "ok", "reset": 0, "placeholders_recovered": 0}

    def test_stuck_processing_batch_is_reset(self) -> None:
        from scans.models import ScanBatch
        from scans.tasks import reset_stuck_scan_batches_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        # Manually set updated_at to > 1 hour ago
        ScanBatch.objects.filter(pk=batch.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        result = reset_stuck_scan_batches_task()
        assert result["reset"] == 1

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_FAILED

    def test_recent_processing_batch_is_not_reset(self) -> None:
        from scans.models import ScanBatch
        from scans.tasks import reset_stuck_scan_batches_task

        ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        result = reset_stuck_scan_batches_task()
        assert result["reset"] == 0


@pytest.mark.django_db()
class TestAutoRetryFailedScanBatchesTask:
    """Tests for auto_retry_failed_scan_batches_task."""

    def test_no_failed_batches_returns_zero(self) -> None:
        from scans.tasks import auto_retry_failed_scan_batches_task

        result = auto_retry_failed_scan_batches_task()
        assert result == {"status": "ok", "retried": 0}

    def test_returns_retried_count_for_failed_batches(self) -> None:
        from scans.models import ScanBatch, ScanPlaceholder
        from scans.tasks import auto_retry_failed_scan_batches_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_FAILED)
        # Create a failed placeholder using factory
        ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
        )

        with patch("scans.tasks.retry_failed_scans_task") as mock_retry:
            mock_retry.delay = MagicMock()
            result = auto_retry_failed_scan_batches_task()

        assert result["retried"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Mark batch failed helper
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestMarkBatchFailed:
    """Tests for _mark_batch_failed helper."""

    def test_marks_existing_batch_as_failed(self) -> None:
        from scans.models import ScanBatch
        from scans.tasks import _mark_batch_failed

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        _mark_batch_failed(str(batch.id), "Test error")

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_FAILED

    def test_handles_nonexistent_batch_gracefully(self) -> None:
        from scans.tasks import _mark_batch_failed

        # Should not raise
        _mark_batch_failed(str(uuid.uuid4()), "Test error")


# ──────────────────────────────────────────────────────────────────────────────
# watch_r2_scan_folders_task
# ──────────────────────────────────────────────────────────────────────────────


class TestWatchR2ScanFoldersTask:
    """Tests for watch_r2_scan_folders_task."""

    def test_returns_service_result_with_no_new_folders(self) -> None:
        from scans.tasks import watch_r2_scan_folders_task

        service_result = {
            "new_folders": [],
            "skipped": 0,
            "ingested": 0,
            "results": [],
        }
        with patch(
            "scans.scan_folder.ScanFolderWatcherService.discover_and_ingest_all",
            return_value=service_result,
        ):
            result = watch_r2_scan_folders_task()

        assert result["new_folders"] == []

    @pytest.mark.django_db()
    def test_notifies_staff_when_new_folders_found(self, staff_user: object) -> None:
        from scans.tasks import watch_r2_scan_folders_task

        service_result = {
            "new_folders": [
                {
                    "r2_prefix": "ScanOutput/2024-01-01/",
                    "file_count": 10,
                    "campaign_name": "Test Campaign",
                    "is_valid": True,
                    "notification_type": "info",
                    "appeal_code": "AP001",
                    "payment_method": "cash",
                },
            ],
            "skipped": 0,
            "ingested": 1,
            "results": [],
        }
        with patch(
            "scans.scan_folder.ScanFolderWatcherService.discover_and_ingest_all",
            return_value=service_result,
        ):
            result = watch_r2_scan_folders_task()

        assert len(result["new_folders"]) == 1

    @pytest.mark.django_db()
    def test_return_value_is_json_serializable_when_campaign_resolved(self) -> None:
        """Regression: Celery's JSON result backend must be able to encode the
        task's return value even when folders resolve to a live Campaign."""
        import json

        from campaigns.models import Campaign
        from scans.tasks import watch_r2_scan_folders_task
        from tests.factories import CampaignFactory

        campaign: Campaign = CampaignFactory()
        service_result = {
            "new_folders": [
                {
                    "r2_prefix": "ScanOutput/X/AP001/cash/",
                    "file_count": 3,
                    "campaign": campaign,  # simulates scan_folder._build_pending_folder_entry
                    "campaign_name": f"{campaign.client.name} — {campaign.name}",
                    "is_valid": True,
                    "notification_type": "info",
                    "appeal_code": "AP001",
                    "payment_method": "cash",
                },
            ],
            "skipped": 0,
            "ingested": 0,
            "results": [],
        }
        with patch(
            "scans.scan_folder.ScanFolderWatcherService.discover_and_ingest_all",
            return_value=service_result,
        ):
            result = watch_r2_scan_folders_task()

        # Must not raise TypeError('Object of type Campaign is not JSON serializable')
        json.dumps(result)
        assert "campaign" not in result["new_folders"][0]
        assert result["new_folders"][0]["campaign_name"].endswith(campaign.name)


# ──────────────────────────────────────────────────────────────────────────────
# _notify_staff_of_new_scan_folders
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestNotifyStaffOfNewScanFolders:
    """Tests for _notify_staff_of_new_scan_folders helper."""

    def test_creates_notifications_for_valid_folder(self, staff_user: object) -> None:
        from notifications.models import Notification
        from scans.tasks import _notify_staff_of_new_scan_folders

        initial_count = Notification.objects.count()
        _notify_staff_of_new_scan_folders(
            [
                {
                    "r2_prefix": "ScanOutput/2024-01-01/",
                    "file_count": 5,
                    "campaign_name": "Test Campaign",
                    "is_valid": True,
                    "notification_type": Notification.TYPE_INFO,
                    "appeal_code": "AP001",
                    "payment_method": "cash",
                }
            ]
        )
        assert Notification.objects.count() > initial_count

    def test_creates_notifications_for_invalid_folder(self, staff_user: object) -> None:
        from notifications.models import Notification
        from scans.tasks import _notify_staff_of_new_scan_folders

        initial_count = Notification.objects.count()
        _notify_staff_of_new_scan_folders(
            [
                {
                    "r2_prefix": "ScanOutput/unknown-folder/",
                    "file_count": 3,
                    "campaign_name": "Unknown",
                    "is_valid": False,
                    "notification_type": Notification.TYPE_WARNING,
                }
            ]
        )
        assert Notification.objects.count() > initial_count

    def test_handles_no_staff_users_gracefully(self) -> None:
        """When no staff users exist, should not crash."""
        from core.models import User
        from scans.tasks import _notify_staff_of_new_scan_folders

        User.objects.filter(is_staff=True).update(is_active=False)
        # Should not raise
        _notify_staff_of_new_scan_folders(
            [
                {
                    "r2_prefix": "ScanOutput/test/",
                    "file_count": 1,
                    "campaign_name": "Test",
                    "is_valid": True,
                    "notification_type": "info",
                }
            ]
        )


# ──────────────────────────────────────────────────────────────────────────────
# process_scan_batch_task — happy + error paths
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestProcessScanBatchTaskPaths:
    """Tests for process_scan_batch_task success and non-retryable paths.

    Marked ``django_db`` because the orchestrator opens a
    ``transaction.atomic`` block (so chord dispatch can hook
    ``transaction.on_commit``); even fully-mocked code paths need a real DB
    connection to read the autocommit state.
    """

    def test_no_pending_placeholders_finalises_empty(self) -> None:
        from scans.tasks import process_scan_batch_task

        with (
            patch(
                "scans.scan_processing.ScanProcessingService.prepare_scan_batch",
                return_value=(MagicMock(), [], []),
            ),
            patch("scans.tasks._finalise_empty_batch") as mock_finalise,
        ):
            mock_finalise.return_value = {"status": "completed", "total": 0}
            process_scan_batch_task(str(uuid.uuid4()))

        mock_finalise.assert_called_once()

    def test_non_retryable_error_marks_batch_failed(self) -> None:
        from django.core.exceptions import ValidationError

        from scans.tasks import process_scan_batch_task

        with (
            patch(
                "scans.scan_processing.ScanProcessingService.prepare_scan_batch",
                side_effect=ValidationError("Bad batch"),
            ),
            patch("scans.tasks._mark_batch_failed") as mock_fail,
        ):
            result = process_scan_batch_task(str(uuid.uuid4()))

        mock_fail.assert_called_once()
        assert result["status"] == "error"
        assert result["retryable"] is False


class TestProcessSingleScanTaskPaths:
    """Tests for process_single_scan_task non-retryable path."""

    def test_non_retryable_error_returns_error_dict(self) -> None:
        from django.core.exceptions import ValidationError

        from scans.tasks import process_single_scan_task

        with patch(
            "scans.scan_processing.ScanProcessingService.process_single_scan",
            side_effect=ValidationError("Bad scan"),
        ):
            result = process_single_scan_task(str(uuid.uuid4()))

        assert result["status"] == "error"
        assert result["retryable"] is False

    def test_success_with_batch_id_increments_progress(self) -> None:
        from scans.tasks import process_single_scan_task

        scan_result = {"ocr_status": "matched", "status": "ok"}
        batch_id = str(uuid.uuid4())
        placeholder_id = str(uuid.uuid4())

        with (
            patch(
                "scans.scan_processing.ScanProcessingService.process_single_scan",
                return_value=scan_result,
            ),
            patch("scans.tasks._increment_batch_progress") as mock_incr,
        ):
            result = process_single_scan_task(placeholder_id, batch_id)

        mock_incr.assert_called_once_with(batch_id, placeholder_id, "matched")
        assert result == scan_result
