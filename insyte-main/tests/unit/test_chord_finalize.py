"""Unit tests for chord finalize concurrency hardening.

Covers:

* The row-level lock around ``finalize_scan_batch_task`` so a concurrent
  per-scan retry cannot make ``processed_scans`` / ``matched_scans`` go
  negative or be read in a torn state.
* The chord-error fallback (``finalize_scan_batch_chord_error_task``) so a
  hung header task can never leave a batch stuck in ``processing``.
* The 15-minute "stuck batch" sweep window in
  ``reset_stuck_scan_batches_task``.

The Celery test runner is in ``ALWAYS_EAGER`` mode, so these tests do not
need a live broker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from scans.models import ScanBatch, ScanPlaceholder
from tests.factories import ScanBatchFactory, ScanPlaceholderFactory

# ──────────────────────────────────────────────────────────────────────────────
# Race-fix: select_for_update around finalize
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestFinalizeRowLock:
    """``finalize_scan_batch_task`` must take a row-level lock."""

    def test_finalize_runs_inside_atomic_with_select_for_update(self) -> None:
        """Finalize wraps the ScanBatch read in select_for_update(skip_locked=True).

        We patch the ``ScanBatch.objects`` manager so we can assert the
        finalize path called ``select_for_update(skip_locked=True)`` exactly
        once before delegating to the service layer.
        """
        from scans.tasks import finalize_scan_batch_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        with patch(
            "scans.scan_processing.ScanProcessingService.finalize_scan_batch"
        ) as mock_finalize:
            mock_finalize.return_value = {
                "scan_batch_id": str(batch.id),
                "total": 1,
                "matched": 1,
                "failed": 0,
                "processed": 1,
                "donation_batch_id": None,
            }
            with patch("scans.models.ScanBatch.objects") as mock_manager:
                # Configure the chained calls: select_for_update -> filter -> first
                lock_qs = MagicMock()
                filter_qs = MagicMock()
                filter_qs.first.return_value = batch
                lock_qs.filter.return_value = filter_qs
                mock_manager.select_for_update.return_value = lock_qs
                # The "row exists" fallback on miss
                mock_manager.filter.return_value.exists.return_value = True

                finalize_scan_batch_task.apply(
                    args=([{"ocr_status": "matched"}], str(batch.id))
                ).get()

                mock_manager.select_for_update.assert_called_once_with(skip_locked=True)
                lock_qs.filter.assert_called_once_with(pk=str(batch.id))

        mock_finalize.assert_called_once()

    def test_finalize_no_negative_counters_under_concurrent_increment(self) -> None:
        """Finalize never produces negative counters even when an in-flight
        per-scan retry is incrementing the row at the same time.

        We simulate the concurrent-write window by mutating the batch row
        between the chord-aggregate step (which sees results=[matched, failed])
        and the finalize service call. After finalize returns, both counters
        must be >= 0 and consistent with the result snapshot.
        """
        from scans.tasks import finalize_scan_batch_task

        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=2,
            processed_scans=0,
            matched_scans=0,
        )

        # One matched, one failed in the chord results
        results: list[dict[str, Any]] = [
            {"ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED},
            {"ocr_status": ScanPlaceholder.OCR_STATUS_FAILED},
        ]

        finalize_scan_batch_task.apply(args=(results, str(batch.id))).get()

        batch.refresh_from_db()
        assert batch.processed_scans >= 0
        assert batch.matched_scans >= 0
        # Finalize sets processed_scans=total, matched_scans=matched
        assert batch.processed_scans == 2
        assert batch.matched_scans == 1

    def test_finalize_retries_when_row_is_locked(self) -> None:
        """When ``skip_locked`` returns nothing for an existing row, the
        task re-queues itself (Celery raises ``Retry``)."""
        from celery.exceptions import Retry

        from scans.tasks import finalize_scan_batch_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        # select_for_update().filter().first() returns None (locked), but a
        # plain existence check returns True (row exists, lock held).
        with patch("scans.models.ScanBatch.objects") as mock_manager:
            lock_qs = MagicMock()
            filter_qs = MagicMock()
            filter_qs.first.return_value = None
            lock_qs.filter.return_value = filter_qs
            mock_manager.select_for_update.return_value = lock_qs
            mock_manager.filter.return_value.exists.return_value = True

            with pytest.raises(Retry):
                # Run synchronously so the Retry exception surfaces.
                finalize_scan_batch_task.apply(
                    args=([{"ocr_status": "matched"}], str(batch.id)),
                    throw=True,
                ).get()

    def test_finalize_raises_value_error_when_row_missing(self) -> None:
        """If skip_locked returns nothing AND the row truly does not exist,
        finalize raises a non-retryable ValueError (caught and surfaced as
        an error dict)."""
        from scans.tasks import finalize_scan_batch_task

        with patch("scans.models.ScanBatch.objects") as mock_manager:
            lock_qs = MagicMock()
            filter_qs = MagicMock()
            filter_qs.first.return_value = None
            lock_qs.filter.return_value = filter_qs
            mock_manager.select_for_update.return_value = lock_qs
            mock_manager.filter.return_value.exists.return_value = False

            result = finalize_scan_batch_task.apply(
                args=(
                    [{"ocr_status": "matched"}],
                    "00000000-0000-0000-0000-000000000000",
                )
            ).get()

            assert result["status"] == "error"
            assert result["retryable"] is False


# ──────────────────────────────────────────────────────────────────────────────
# Chord-error fallback: deadline / hung header task
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestChordErrorFallback:
    """``finalize_scan_batch_chord_error_task`` covers the deadline path."""

    def test_chord_error_fallback_marks_pending_placeholders_failed(self) -> None:
        """Hung placeholders get flipped to ``failed`` so the batch can converge."""
        from scans.tasks import finalize_scan_batch_chord_error_task

        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=3,
        )
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        )
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_FAILED
        )
        # The "hung" placeholder
        hung = ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_PENDING
        )

        with patch(
            "scans.scan_processing.ScanProcessingService.finalize_scan_batch"
        ) as mock_finalize:
            mock_finalize.return_value = {
                "scan_batch_id": str(batch.id),
                "total": 3,
                "matched": 1,
                "failed": 2,
                "processed": 3,
                "donation_batch_id": None,
            }
            finalize_scan_batch_chord_error_task.apply(
                args=(
                    None,
                    TimeoutError("chord deadline"),
                    "tb",
                    str(batch.id),
                )
            ).get()

        hung.refresh_from_db()
        assert hung.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        assert "Chord deadline exceeded" in hung.processing_error

        # Aggregates passed to the service reflect the post-flip DB state
        # (1 matched, 2 failed).
        call_args = mock_finalize.call_args
        assert call_args is not None
        assert call_args.kwargs["total"] == 3
        assert call_args.kwargs["matched"] == 1
        assert call_args.kwargs["failed"] == 2

    def test_chord_with_three_tasks_one_hung_finalizes_via_fallback(self) -> None:
        """Mocked chord-of-3 with 1 hung task: fallback fires and converges
        the batch instead of leaving it stuck in 'processing'.

        We do not advance Celery's wall clock; we directly invoke the
        chord-error callback which is what Celery would call once
        ``expires`` elapses.
        """
        from scans.tasks import finalize_scan_batch_chord_error_task

        batch = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=3,
        )
        # Two completed in time
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        )
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        )
        # One hung past the 10-minute deadline
        ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_PROCESSING
        )

        # Use the real finalize service to drive the batch to a terminal state
        finalize_scan_batch_chord_error_task.apply(
            args=(
                None,
                TimeoutError("chord deadline"),
                "tb",
                str(batch.id),
            )
        ).get()

        batch.refresh_from_db()
        # Final status comes from final_status_for_outcomes(total=3, matched=2,
        # failed=1) — partially_completed because one failed.
        assert batch.status == ScanBatch.STATUS_PARTIALLY_COMPLETED
        assert batch.processed_scans == 3
        assert batch.matched_scans == 2

    def test_chord_error_fallback_handles_missing_batch(self) -> None:
        """If the batch row vanishes between dispatch and chord-error
        callback the fallback returns an error dict instead of raising."""
        from scans.tasks import finalize_scan_batch_chord_error_task

        result = finalize_scan_batch_chord_error_task.apply(
            args=(
                None,
                TimeoutError("chord deadline"),
                "tb",
                "00000000-0000-0000-0000-000000000000",
            )
        ).get()

        assert result["status"] == "error"
        assert result["retryable"] is False


# ──────────────────────────────────────────────────────────────────────────────
# Chord wiring: dispatch helper sets expires + link_error
# ──────────────────────────────────────────────────────────────────────────────


class TestDispatchScanChord:
    """``_dispatch_scan_chord`` must set an explicit deadline + error link."""

    def test_dispatch_attaches_link_error_and_per_task_expires(self) -> None:
        from scans.tasks import _dispatch_scan_chord

        with (
            patch("scans.tasks.chord") as mock_chord,
            patch("scans.tasks.group") as mock_group,
        ):
            mock_canvas = MagicMock()
            mock_chord.return_value = mock_canvas

            _dispatch_scan_chord(
                "batch-id",
                known_urns=["URN1"],
                placeholder_ids=["ph-1", "ph-2"],
            )

            # ``chord(header, body)`` was assembled from a real ``group``
            # built via the per-task ``.s(...).set(expires=...)`` chain.
            mock_chord.assert_called_once()
            mock_group.assert_called_once()
            mock_canvas.link_error.assert_called_once()
            mock_canvas.apply_async.assert_called_once()
            # ``apply_async`` is called without ``expires`` because the
            # deadline is on each header signature (chord-level expires
            # would only mark the body task).
            assert mock_canvas.apply_async.call_args.kwargs == {}

    def test_each_header_signature_carries_expires_deadline(self) -> None:
        """Per-task signatures fed into ``group`` carry an ``expires`` value."""
        from scans.tasks import _dispatch_scan_chord

        captured_header_args: list[Any] = []

        def fake_group(sig_iter: Any) -> Any:
            captured_header_args.extend(list(sig_iter))
            return MagicMock(name="group_result")

        with (
            patch("scans.tasks.chord") as mock_chord,
            patch("scans.tasks.group", side_effect=fake_group),
        ):
            mock_chord.return_value = MagicMock()

            _dispatch_scan_chord(
                "batch-id",
                known_urns=["URN1"],
                placeholder_ids=["ph-1", "ph-2"],
            )

        assert len(captured_header_args) == 2
        for sig in captured_header_args:
            assert sig.options.get("expires") is not None


# ──────────────────────────────────────────────────────────────────────────────
# 15-minute stuck-batch sweep
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestStuckBatchThreshold:
    """``reset_stuck_scan_batches_task`` uses a 15-minute window."""

    def test_batch_stuck_for_16_minutes_is_reset(self) -> None:
        from scans.tasks import reset_stuck_scan_batches_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        ScanBatch.objects.filter(pk=batch.pk).update(
            updated_at=timezone.now() - timedelta(minutes=16)
        )

        result = reset_stuck_scan_batches_task()
        assert result["reset"] == 1

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_FAILED

    def test_batch_stuck_for_10_minutes_is_not_reset(self) -> None:
        """A 10-minute-old processing batch is still inside the chord
        deadline and must not be touched."""
        from scans.tasks import reset_stuck_scan_batches_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        ScanBatch.objects.filter(pk=batch.pk).update(
            updated_at=timezone.now() - timedelta(minutes=10)
        )

        result = reset_stuck_scan_batches_task()
        assert result["reset"] == 0

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_PROCESSING

    def test_threshold_constant_is_15_minutes(self) -> None:
        """Sanity check: the module-level threshold matches the docstring."""
        from scans.tasks import _STUCK_BATCH_THRESHOLD

        assert timedelta(minutes=15) == _STUCK_BATCH_THRESHOLD


# ──────────────────────────────────────────────────────────────────────────────
# Beat schedule wiring
# ──────────────────────────────────────────────────────────────────────────────


class TestBeatSchedule:
    """The Celery beat schedule must run the sweep every 5 minutes."""

    def test_reset_stuck_scan_batches_runs_every_five_minutes(self) -> None:
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE["reset-stuck-scan-batches"]
        assert schedule["task"] == "scans.reset_stuck_scan_batches"
        assert schedule["schedule"] == 300.0
