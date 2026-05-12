"""Regression tests for ScanBatch progress increment idempotency.

When ``process_single_scan_task`` is retried by Celery — for example after a
transient Document AI failure — the per-task ``_increment_batch_progress``
call previously bumped ``processed_scans`` and ``matched_scans`` once per
attempt rather than once per placeholder, so retries silently corrupted batch
progress. The fix records each applied placeholder UUID in
``ScanBatch.processed_placeholder_ids`` and short-circuits subsequent calls
for the same key.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scans.models import ScanBatch, ScanPlaceholder
from scans.tasks import _increment_batch_progress, process_single_scan_task
from tests.factories import ScanBatchFactory, ScanPlaceholderFactory


@pytest.mark.django_db
class TestIncrementBatchProgressIdempotency:
    """Direct tests of the ``_increment_batch_progress`` guard."""

    def test_first_call_increments_counters_and_records_key(self) -> None:
        """A fresh placeholder bumps both counters and is recorded as applied."""
        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=2,
            processed_scans=0,
            matched_scans=0,
        )
        placeholder = ScanPlaceholderFactory(batch=batch)

        _increment_batch_progress(
            str(batch.id), str(placeholder.id), ScanPlaceholder.OCR_STATUS_MATCHED
        )

        batch.refresh_from_db()
        assert batch.processed_scans == 1
        assert batch.matched_scans == 1
        assert str(placeholder.id) in batch.processed_placeholder_ids

    def test_duplicate_call_is_a_noop(self) -> None:
        """A second call for the same placeholder must not bump counters."""
        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=2,
            processed_scans=0,
            matched_scans=0,
        )
        placeholder = ScanPlaceholderFactory(batch=batch)

        for _ in range(3):
            _increment_batch_progress(
                str(batch.id),
                str(placeholder.id),
                ScanPlaceholder.OCR_STATUS_MATCHED,
            )

        batch.refresh_from_db()
        assert batch.processed_scans == 1
        assert batch.matched_scans == 1
        assert batch.processed_placeholder_ids == [str(placeholder.id)]

    def test_distinct_placeholders_each_increment(self) -> None:
        """Different placeholders each get counted exactly once."""
        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=3,
            processed_scans=0,
            matched_scans=0,
        )
        ph_a = ScanPlaceholderFactory(batch=batch)
        ph_b = ScanPlaceholderFactory(batch=batch)
        ph_c = ScanPlaceholderFactory(batch=batch)

        _increment_batch_progress(
            str(batch.id), str(ph_a.id), ScanPlaceholder.OCR_STATUS_MATCHED
        )
        _increment_batch_progress(
            str(batch.id), str(ph_b.id), ScanPlaceholder.OCR_STATUS_FAILED
        )
        _increment_batch_progress(
            str(batch.id), str(ph_c.id), ScanPlaceholder.OCR_STATUS_MATCHED
        )

        batch.refresh_from_db()
        assert batch.processed_scans == 3
        assert batch.matched_scans == 2
        assert set(batch.processed_placeholder_ids) == {
            str(ph_a.id),
            str(ph_b.id),
            str(ph_c.id),
        }

    def test_missing_batch_is_logged_and_skipped(self) -> None:
        """A vanished ScanBatch row must not raise — just log and exit."""
        import uuid as _uuid

        # No batch with this id exists; call must be a benign no-op.
        _increment_batch_progress(
            str(_uuid.uuid4()),
            str(_uuid.uuid4()),
            ScanPlaceholder.OCR_STATUS_MATCHED,
        )


@pytest.mark.django_db
class TestProcessSingleScanTaskRetryIdempotency:
    """End-to-end retry idempotency through ``process_single_scan_task``."""

    def test_retried_task_increments_progress_only_once(self) -> None:
        """Re-running ``process_single_scan_task`` for the same placeholder
        must not double-count the batch progress, mirroring a Celery retry
        after a transient Document AI failure that ultimately succeeds on
        the second attempt.
        """
        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=1,
            processed_scans=0,
            matched_scans=0,
        )
        placeholder = ScanPlaceholderFactory(batch=batch)

        scan_result = {
            "ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED,
            "status": "ok",
        }

        # Two successful invocations for the same (batch, placeholder).
        # The second simulates a Celery retry that lands after the first
        # attempt already incremented the counter.
        with patch(
            "scans.scan_processing.ScanProcessingService.process_single_scan",
            return_value=scan_result,
        ):
            process_single_scan_task.run(
                placeholder_id=str(placeholder.id),
                scan_batch_id=str(batch.id),
            )
            process_single_scan_task.run(
                placeholder_id=str(placeholder.id),
                scan_batch_id=str(batch.id),
            )

        batch.refresh_from_db()
        assert batch.processed_scans == 1, "Retry must not double-count processed_scans"
        assert batch.matched_scans == 1, "Retry must not double-count matched_scans"
        assert batch.processed_placeholder_ids == [str(placeholder.id)]
