"""Unit tests for scan processing Celery tasks.

Ensures task logic correctly captures status and queries DB.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from scans.models import ScanBatch, ScanPlaceholder
from scans.tasks import (
    create_scan_batch_from_r2_task,
    process_scan_batch_task,
    process_single_scan_task,
    retry_failed_scans_task,
)
from tests.factories import (
    DonationFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
)


@pytest.mark.django_db
class TestRetryFailedScansTask:
    """Tests for retry_failed_scans_task."""

    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_retry_with_no_failed_scans(self, mock_process: Any) -> None:
        """SCAN-TASK-UNIT-002: Returns 0 when no scans are failed."""
        batch = ScanBatchFactory(status=ScanBatch.STATUS_FAILED)
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        )

        result = retry_failed_scans_task(str(batch.id))

        assert result == {
            "status": "no_failed_scans",
            "retried": 0,
            "skipped_with_existing_donation": [],
        }
        mock_process.assert_not_called()

    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_retry_captures_ids_before_reset(self, mock_process: Any) -> None:
        """SCAN-TASK-UNIT-001 & 003: IDs are captured before status reset.

        This ensures the bug where values_list returns empty after .update()
        is fixed and all items are processed.
        """
        batch = ScanBatchFactory(status=ScanBatch.STATUS_FAILED)
        ph1 = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error="Error 1",
        )
        ph2 = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error="Error 2",
        )

        # Mock the service to just return a result
        mock_process.return_value = {"ocr_status": "matched"}

        result = retry_failed_scans_task(str(batch.id))

        assert result == {
            "status": "retried",
            "retried": 2,
            "total_failed": 2,
            "skipped_with_existing_donation": [],
        }
        assert mock_process.call_count == 2

        # Verify both IDs were passed to the processor
        called_ids = [call.args[0] for call in mock_process.call_args_list]
        assert str(ph1.id) in called_ids
        assert str(ph2.id) in called_ids

        # Verify items were reset in DB
        ph1.refresh_from_db()
        assert ph1.ocr_status == ScanPlaceholder.OCR_STATUS_PENDING
        assert ph1.processing_error == ""

    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_retry_skips_placeholder_with_existing_donation(
        self, mock_process: Any
    ) -> None:
        """SCAN-TASK-UNIT-004: Failed placeholders that already have a Donation
        are NOT reset and NOT re-processed, so retries cannot create duplicate
        donations. The skipped IDs are reported in the task result.
        """
        batch = ScanBatchFactory(status=ScanBatch.STATUS_FAILED)
        existing_donation = DonationFactory()
        ph_with_donation = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error="Donor match failed after donation captured",
            donation=existing_donation,
        )
        ph_without_donation = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error="OCR error",
        )
        mock_process.return_value = {"ocr_status": "matched"}

        result = retry_failed_scans_task(str(batch.id))

        assert result["status"] == "retried"
        assert result["retried"] == 1
        assert result["total_failed"] == 2
        assert result["skipped_with_existing_donation"] == [str(ph_with_donation.id)]

        mock_process.assert_called_once_with(str(ph_without_donation.id))

        ph_with_donation.refresh_from_db()
        assert ph_with_donation.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        assert (
            ph_with_donation.processing_error
            == "Donor match failed after donation captured"
        )
        assert ph_with_donation.donation_id == existing_donation.id

        original_amount = existing_donation.amount
        existing_donation.refresh_from_db()
        assert existing_donation.amount == original_amount

    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_retry_when_all_failed_have_existing_donations(
        self, mock_process: Any
    ) -> None:
        """SCAN-TASK-UNIT-005: If every failed placeholder already has a donation,
        the task short-circuits with status=skipped_all and never resets anything.
        """
        batch = ScanBatchFactory(status=ScanBatch.STATUS_FAILED)
        ph = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_FAILED,
            processing_error="post-donation failure",
            donation=DonationFactory(),
        )

        result = retry_failed_scans_task(str(batch.id))

        assert result == {
            "status": "skipped_all",
            "retried": 0,
            "total_failed": 1,
            "skipped_with_existing_donation": [str(ph.id)],
        }
        mock_process.assert_not_called()

        ph.refresh_from_db()
        assert ph.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED


@pytest.mark.django_db
class TestCreateScanBatchFromR2Task:
    """Tests for create_scan_batch_from_r2_task."""

    @patch("scans.tasks._filter_image_keys")
    @patch("scans.tasks._resolve_user")
    @patch("core.storage_backends.r2_list_prefix")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_task_returns_validation_error_without_retry(
        self,
        mock_create_scan_batch: Any,
        mock_list_prefix: Any,
        mock_resolve_user: Any,
        mock_filter_image_keys: Any,
    ) -> None:
        """Expected validation failures should not be retried by Celery."""
        mock_list_prefix.return_value = ["ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf"]
        mock_filter_image_keys.return_value = [
            "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf"
        ]
        mock_resolve_user.return_value = None
        mock_create_scan_batch.side_effect = ValueError(
            "Each physical batch must be uploaded as exactly one PDF file."
        )

        result = create_scan_batch_from_r2_task.run(
            campaign_id="campaign-1",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )

        assert result == {
            "status": "error",
            "message": "Each physical batch must be uploaded as exactly one PDF file.",
            "retryable": False,
        }

    @patch("scans.tasks._filter_image_keys")
    @patch("scans.tasks._resolve_user")
    @patch("core.storage_backends.r2_list_prefix")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_task_returns_validation_error_for_django_validation_failures(
        self,
        mock_create_scan_batch: Any,
        mock_list_prefix: Any,
        mock_resolve_user: Any,
        mock_filter_image_keys: Any,
    ) -> None:
        """Model validation failures should not be retried by Celery."""
        mock_list_prefix.return_value = ["ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf"]
        mock_filter_image_keys.return_value = [
            "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf"
        ]
        mock_resolve_user.return_value = None
        mock_create_scan_batch.side_effect = ValidationError(
            "Invalid scan batch combination"
        )

        result = create_scan_batch_from_r2_task.run(
            campaign_id="campaign-1",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )

        assert result == {
            "status": "error",
            "message": "['Invalid scan batch combination']",
            "retryable": False,
        }

    @patch("scans.tasks._resolve_user")
    @patch("core.storage_backends.r2_list_prefix")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_task_returns_no_files_when_listing_succeeds_empty(
        self,
        mock_create_scan_batch: Any,
        mock_list_prefix: Any,
        mock_resolve_user: Any,
    ) -> None:
        """A successful list with zero matches must remain a no_files response."""
        mock_list_prefix.return_value = []
        mock_resolve_user.return_value = None

        result = create_scan_batch_from_r2_task.run(
            campaign_id="campaign-1",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )

        assert result["status"] == "no_files"
        mock_create_scan_batch.assert_not_called()

    @patch("scans.tasks.create_scan_batch_from_r2_task.retry")
    @patch("core.storage_backends.r2_list_prefix")
    def test_task_retries_on_transient_r2_outage(
        self,
        mock_list_prefix: Any,
        mock_retry: Any,
    ) -> None:
        """A ClientError from R2 (e.g. 5xx InternalError) must trigger Celery retry,
        not be silently masked as no_files."""
        from botocore.exceptions import ClientError

        mock_list_prefix.side_effect = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "We're sorry"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "ListObjectsV2",
        )
        mock_retry.side_effect = RuntimeError("retry requested")

        with pytest.raises(RuntimeError, match="retry requested"):
            create_scan_batch_from_r2_task.run(
                campaign_id="campaign-1",
                r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
            )

        mock_retry.assert_called_once()

    @patch("scans.tasks.create_scan_batch_from_r2_task.retry")
    @patch("core.storage_backends.r2_list_prefix")
    def test_task_does_not_retry_on_r2_misconfiguration(
        self,
        mock_list_prefix: Any,
        mock_retry: Any,
    ) -> None:
        """403 AccessDenied is misconfig — return non-retryable error."""
        from core.storage_backends import R2ConfigurationError

        mock_list_prefix.side_effect = R2ConfigurationError(
            "R2 list failed (non-retryable): AccessDenied"
        )

        result = create_scan_batch_from_r2_task.run(
            campaign_id="campaign-1",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )

        assert result == {
            "status": "error",
            "message": "R2 list failed (non-retryable): AccessDenied",
            "retryable": False,
        }
        mock_retry.assert_not_called()

    @patch("scans.tasks.process_scan_batch_task.delay")
    @patch("scans.tasks._filter_image_keys")
    @patch("scans.tasks._resolve_user")
    @patch("core.storage_backends.r2_list_prefix")
    @patch("scans.scan_processing.ScanProcessingService.create_scan_batch_from_r2")
    def test_task_passes_batch_name_and_returns_total_scans(
        self,
        mock_create_scan_batch: Any,
        mock_list_prefix: Any,
        mock_resolve_user: Any,
        mock_filter_image_keys: Any,
        mock_process_delay: Any,
    ) -> None:
        """Task should forward the canonical batch name and use model totals."""
        mock_list_prefix.return_value = ["ScanOutput/BRC/SPRING25/cheque/Batch-002.pdf"]
        mock_filter_image_keys.return_value = [
            "ScanOutput/BRC/SPRING25/cheque/Batch-002.pdf"
        ]
        mock_resolve_user.return_value = None
        mock_create_scan_batch.return_value = SimpleNamespace(
            id="scan-batch-1",
            total_scans=7,
        )

        result = create_scan_batch_from_r2_task.run(
            campaign_id="campaign-2",
            r2_prefix="ScanOutput/BRC/SPRING25/cheque/",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            batch_name="Batch-002.pdf",
        )

        mock_create_scan_batch.assert_called_once_with(
            campaign_id="campaign-2",
            r2_keys=["ScanOutput/BRC/SPRING25/cheque/Batch-002.pdf"],
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            user=None,
            batch_name="Batch-002.pdf",
        )
        mock_process_delay.assert_called_once_with("scan-batch-1")
        assert result == {
            "status": "created",
            "scan_batch_id": "scan-batch-1",
            "total_scans": 7,
            "processing_triggered": True,
        }


@pytest.mark.django_db
class TestProcessScanTasks:
    """Tests for process_scan_batch_task and process_single_scan_task."""

    @patch("scans.tasks._mark_batch_failed")
    @patch("scans.tasks.process_scan_batch_task.retry")
    @patch("scans.scan_processing.ScanProcessingService.prepare_scan_batch")
    def test_process_scan_batch_does_not_retry_non_retryable_errors(
        self,
        mock_prepare_batch: Any,
        mock_retry: Any,
        mock_mark_failed: Any,
    ) -> None:
        """Missing or invalid batches should return a deterministic error result."""
        mock_prepare_batch.side_effect = ValueError("ScanBatch not found: batch-1")

        result = process_scan_batch_task.run(scan_batch_id="batch-1")

        assert result == {
            "status": "error",
            "scan_batch_id": "batch-1",
            "message": "ScanBatch not found: batch-1",
            "retryable": False,
        }
        mock_mark_failed.assert_called_once_with(
            "batch-1", "ScanBatch not found: batch-1"
        )
        mock_retry.assert_not_called()

    @patch("scans.tasks.process_scan_batch_task.retry")
    @patch("scans.scan_processing.ScanProcessingService.prepare_scan_batch")
    def test_process_scan_batch_retries_unexpected_errors(
        self,
        mock_prepare_batch: Any,
        mock_retry: Any,
    ) -> None:
        """Unexpected task failures should still go through Celery retry."""
        mock_prepare_batch.side_effect = RuntimeError("temporary OCR outage")
        mock_retry.side_effect = RuntimeError("retry requested")

        with pytest.raises(RuntimeError, match="retry requested"):
            process_scan_batch_task.run(scan_batch_id="batch-2")

        mock_retry.assert_called_once()

    @patch("scans.tasks.process_single_scan_task.retry")
    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_process_single_scan_does_not_retry_non_retryable_errors(
        self,
        mock_process_single: Any,
        mock_retry: Any,
    ) -> None:
        """Missing placeholders should not be retried by Celery."""
        mock_process_single.side_effect = ValueError(
            "ScanPlaceholder not found: placeholder-1"
        )

        result = process_single_scan_task.run(placeholder_id="placeholder-1")

        assert result == {
            "status": "error",
            "placeholder_id": "placeholder-1",
            "message": "ScanPlaceholder not found: placeholder-1",
            "retryable": False,
        }
        mock_retry.assert_not_called()

    @patch("scans.tasks.process_single_scan_task.retry")
    @patch("scans.scan_processing.ScanProcessingService.process_single_scan")
    def test_process_single_scan_retries_unexpected_errors(
        self,
        mock_process_single: Any,
        mock_retry: Any,
    ) -> None:
        """Unexpected single-scan failures should still use Celery retry."""
        mock_process_single.side_effect = RuntimeError("temporary OCR outage")
        mock_retry.side_effect = RuntimeError("retry requested")

        with pytest.raises(RuntimeError, match="retry requested"):
            process_single_scan_task.run(placeholder_id="placeholder-2")

        mock_retry.assert_called_once()
