"""Hardening tests for the post-batch-creation → pre-QA scan pipeline.

These tests cover the eight findings from the ``after-the-batches`` review:

1. Transient OCR errors re-raise so Celery retries actually fire.
2. ``finalize_scan_batch`` / ``create_donation_batch`` is idempotent.
3. Stuck PROCESSING placeholders are recovered by the sweeper.
4. Orchestrator wraps PROCESSING save + chord dispatch in
   ``transaction.atomic`` + ``on_commit``.
5. Empty-r2_keys outcome surfaces on ``ScanUploadProgress``.
6. (Covered indirectly via the idempotency test — recompute path.)
7. (Folded into Finding 1 — atomic wrapping of success path.)
8. Finalize retry exhaustion dispatches the chord-error fallback.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from donations.models import DonationBatch
from scans.document_ai import TransientOCRError
from scans.models import ScanBatch, ScanPlaceholder, ScanUploadProgress
from scans.tasks import (
    _maybe_dispatch_finalize_fallback,
    _record_no_files_failure,
    create_scan_batch_from_r2_task,
    process_scan_batch_task,
    process_single_scan_task,
)
from tests.factories import (
    CampaignFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
)

# ─────────────────────────────────────────────────────────────────────────────
# Finding 1 — transient errors re-raise so Celery retries fire
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestTransientErrorRetryPath:
    """``TransientOCRError`` from OCR/storage must propagate to Celery retry."""

    def test_transient_ocr_error_triggers_celery_retry(self) -> None:
        """Within retry budget, the task calls ``self.retry`` rather than
        marking the placeholder FAILED on the first attempt.
        """
        from celery.exceptions import Retry

        placeholder = ScanPlaceholderFactory()
        with (
            patch(
                "scans.scan_processing.ScanProcessingService.process_single_scan",
                side_effect=TransientOCRError("Document AI 503"),
            ),
            pytest.raises((Retry, TransientOCRError)),
        ):
            # eager mode: ``self.retry`` raises ``Retry`` while retries
            # remain; the underlying exception only escapes after the budget
            # is exhausted.
            process_single_scan_task.apply(
                args=(str(placeholder.id), str(placeholder.batch_id), [])
            ).get()

        placeholder.refresh_from_db()
        # On non-final retry attempts the placeholder must NOT be marked
        # FAILED — it stays in PROCESSING so a successful retry can complete
        # the work.
        assert placeholder.ocr_status != ScanPlaceholder.OCR_STATUS_FAILED

    def test_transient_error_marks_failed_after_retries_exhausted(
        self, settings: Any
    ) -> None:
        """Once retries are exhausted, the task marks the placeholder FAILED
        before the exception escapes — so the chord aggregator sees a failed
        scan and the UI converges with the DB.
        """
        placeholder = ScanPlaceholderFactory()
        with (
            patch(
                "scans.scan_processing.ScanProcessingService.process_single_scan",
                side_effect=TransientOCRError("Document AI 503"),
            ),
            patch.object(process_single_scan_task, "max_retries", 0),
            contextlib.suppress(Exception),
        ):
            process_single_scan_task.apply(
                args=(str(placeholder.id), str(placeholder.batch_id), [])
            ).get()

        placeholder.refresh_from_db()
        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        assert "Document AI 503" in placeholder.processing_error


# ─────────────────────────────────────────────────────────────────────────────
# Finding 2 — finalize_scan_batch / create_donation_batch is idempotent
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestFinalizeIdempotency:
    """A second invocation of ``finalize_scan_batch`` for the same batch
    must not raise IntegrityError on the ``(campaign, batch_name)``
    DonationBatch unique constraint and must not duplicate Donations.
    """

    def test_finalize_twice_does_not_create_duplicate_donation_batch(self) -> None:
        from scans.scan_processing import ScanProcessingService

        scan_batch = ScanBatchFactory()
        ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED,
        )

        first = ScanProcessingService.finalize_scan_batch(
            str(scan_batch.id), total=1, matched=1, failed=0
        )
        second = ScanProcessingService.finalize_scan_batch(
            str(scan_batch.id), total=1, matched=1, failed=0
        )

        assert first["donation_batch_id"] == second["donation_batch_id"]
        assert (
            DonationBatch.objects.filter(
                campaign=scan_batch.campaign, batch_name=scan_batch.batch_name
            ).count()
            == 1
        )

    def test_create_donation_batch_links_existing_when_called_twice(self) -> None:
        """``create_donation_batch`` finds the existing row instead of
        creating a duplicate; placeholders without donations are linked.
        """
        from scans.scan_processing_donations import create_donation_batch

        scan_batch = ScanBatchFactory()
        ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED,
        )

        first = create_donation_batch(scan_batch)
        second = create_donation_batch(scan_batch)

        assert first is not None and second is not None
        assert first.pk == second.pk
        assert (
            DonationBatch.objects.filter(
                campaign=scan_batch.campaign, batch_name=scan_batch.batch_name
            ).count()
            == 1
        )

    def test_create_donation_batch_handles_concurrent_create_race(self) -> None:
        """Simulates a "both finalizers miss the lookup" race so the second
        ``.create()`` collides with the unique constraint. The
        ``IntegrityError`` handler must recover the winner's row instead
        of letting the exception escape.
        """
        from django.db import IntegrityError

        from scans.scan_processing_donations import create_donation_batch

        scan_batch = ScanBatchFactory()
        ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED,
        )

        # First call creates the row normally — this is the "winner".
        winner = create_donation_batch(scan_batch)
        assert winner is not None

        # For the second call, force-skip the pre-existing lookup AND make
        # ``.create()`` raise IntegrityError as if a concurrent finalizer
        # beat us to the unique constraint. The recovery path must look the
        # winner back up and continue.
        original_filter = DonationBatch.objects.filter

        def _raise_integrity(*args: Any, **kwargs: Any) -> Any:
            raise IntegrityError("duplicate batch_name")

        # Pre-existing lookup must return None to take the create branch.
        # Recovery lookup (after the IntegrityError) returns the real winner.
        filter_calls = {"count": 0}

        def _staged_filter(*args: Any, **kwargs: Any) -> Any:
            filter_calls["count"] += 1
            if filter_calls["count"] == 1:
                # pre_existing lookup → pretend nothing exists
                empty = MagicMock()
                empty.first.return_value = None
                return empty
            # All later filter() calls (recovery + aggregates) use real qs.
            return original_filter(*args, **kwargs)

        with (
            patch.object(DonationBatch.objects, "create", side_effect=_raise_integrity),
            patch.object(DonationBatch.objects, "filter", side_effect=_staged_filter),
        ):
            second = create_donation_batch(scan_batch)

        assert second is not None
        assert second.pk == winner.pk
        # No duplicate rows created.
        assert (
            DonationBatch.objects.filter(
                campaign=scan_batch.campaign, batch_name=scan_batch.batch_name
            ).count()
            == 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# Finding 3 — stuck PROCESSING placeholders recovered by the sweeper
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestStuckPlaceholderSweep:
    """Placeholders left in PENDING/PROCESSING after a stuck batch is reset
    must be flipped to FAILED so retry sweepers can recover them.
    """

    def test_stuck_processing_placeholders_are_marked_failed(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from scans.tasks import reset_stuck_scan_batches_task

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        ph_processing = ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_PROCESSING
        )
        ph_pending = ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_PENDING
        )
        ph_already_done = ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED
        )

        # Force batch updated_at past the stuck threshold.
        ScanBatch.objects.filter(pk=batch.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        result = reset_stuck_scan_batches_task.run()
        assert result["reset"] == 1
        assert result["placeholders_recovered"] == 2

        ph_processing.refresh_from_db()
        ph_pending.refresh_from_db()
        ph_already_done.refresh_from_db()
        assert ph_processing.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        assert ph_pending.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        # Already-terminal placeholders must NOT be touched.
        assert ph_already_done.ocr_status == ScanPlaceholder.OCR_STATUS_MATCHED


# ─────────────────────────────────────────────────────────────────────────────
# Finding 4 — orchestrator wraps PROCESSING save + chord dispatch atomically
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestOrchestratorTransactionalDispatch:
    """``process_scan_batch_task`` must defer chord dispatch until the
    PROCESSING status save commits, so a failed dispatch doesn't leave the
    batch wedged in PROCESSING with a half-dispatched chord.

    ``transaction=True`` is required so that Django actually commits the
    outermost transaction inside the task — otherwise ``transaction.on_commit``
    callbacks (which is what fires the chord dispatch) would never run.
    """

    def test_chord_dispatch_runs_after_transaction_commit(self) -> None:
        batch = ScanBatchFactory(status=ScanBatch.STATUS_PENDING)
        ph = ScanPlaceholderFactory(
            batch=batch, ocr_status=ScanPlaceholder.OCR_STATUS_PENDING
        )

        with patch("scans.tasks._dispatch_scan_chord") as mock_dispatch:
            process_scan_batch_task.run(str(batch.id))

        # Dispatch must have fired exactly once after the on_commit hook.
        mock_dispatch.assert_called_once()
        called_args = mock_dispatch.call_args[0]
        assert called_args[0] == str(batch.id)
        assert str(ph.id) in called_args[2]

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_PROCESSING


# ─────────────────────────────────────────────────────────────────────────────
# Finding 5 — empty r2_keys surfaces an error on ScanUploadProgress
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestEmptyR2KeysVisible:
    """A scan-completion task that finds no image keys must update
    ``ScanUploadProgress`` so the operator sees the dead end.
    """

    def test_no_files_writes_error_to_progress_row(self) -> None:
        campaign = CampaignFactory()
        ScanUploadProgress.objects.create(campaign=campaign)

        _record_no_files_failure(str(campaign.id), "client/empty/")

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == ScanUploadProgress.STATUS_ERROR
        assert "No image files found" in progress.last_error

    def test_create_scan_batch_no_files_records_failure(self) -> None:
        campaign = CampaignFactory()
        ScanUploadProgress.objects.create(campaign=campaign)

        with patch("core.storage_backends.r2_list_prefix", return_value=[]):
            result = create_scan_batch_from_r2_task.run(
                campaign_id=str(campaign.id),
                r2_prefix="client/empty/",
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
            )

        assert result["status"] == "no_files"
        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.status == ScanUploadProgress.STATUS_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# Finding 8 — finalize retry exhaustion dispatches chord-error fallback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestFinalizeFallbackDispatch:
    """When ``finalize_scan_batch_task`` exhausts retries on a transient
    failure, the chord-error fallback fires so the batch never sits in
    PROCESSING with the DonationBatch uncreated.
    """

    def test_fallback_fires_only_on_final_retry(self) -> None:
        scan_batch_id = "00000000-0000-0000-0000-000000000001"
        task_self = MagicMock()

        # Mid-retry: must NOT dispatch fallback.
        task_self.request.retries = 1
        task_self.max_retries = 5
        with patch(
            "scans.tasks.finalize_scan_batch_chord_error_task.apply_async"
        ) as mock_apply:
            _maybe_dispatch_finalize_fallback(
                task_self, scan_batch_id, RuntimeError("locked")
            )
        mock_apply.assert_not_called()

        # Last retry: must dispatch fallback.
        task_self.request.retries = 5
        task_self.max_retries = 5
        with patch(
            "scans.tasks.finalize_scan_batch_chord_error_task.apply_async"
        ) as mock_apply:
            _maybe_dispatch_finalize_fallback(
                task_self, scan_batch_id, RuntimeError("locked")
            )
        mock_apply.assert_called_once()
        called_args = mock_apply.call_args.kwargs["args"]
        assert called_args[3] == scan_batch_id
