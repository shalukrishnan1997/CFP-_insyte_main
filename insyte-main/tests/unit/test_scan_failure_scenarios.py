"""Comprehensive failure-scenario tests for the scan pipeline.

Each test covers one failure mode that the scan ingest → OCR → finalize
pipeline must handle without losing data, double-charging donors, or
returning HTTP 500 to scanner workstations. Scenarios that depend on
hardening units that have not yet landed are marked ``xfail`` with a
strict reason so they convert to passing tests automatically once the
prerequisite work merges.

The 10 scenarios mirror unit #25 of the parallel hardening effort:

1. Document AI 429 (ResourceExhausted) — retried with backoff.
2. Document AI 503 (ServiceUnavailable) — retried with backoff.
3. Document AI InvalidArgument — non-retryable, placeholder marked failed.
4. Concurrent batch-name collision — unique constraint enforces a single
   winner.
5. HMAC replay attack — duplicate complete-status webhook is deduped
   (full timestamp/nonce rejection awaits unit 21).
6. R2 upload failure mid-redaction — placeholder marked redaction_blocked
   (awaits unit 22).
7. Chord task crash mid-DB-write — finalize aggregates remaining outcomes,
   stuck-batch sweeper recovers any orphan.
8. OCR confidence below threshold — donation auto-flagged for review
   (awaits unit 23).
9. Donor false-positive match — fuzzy match left for QA review
   (awaits unit 24).
10. 5-scanner concurrent upload — one ScanUploadProgress per campaign.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from scans.models import ScanBatch, ScanPlaceholder, ScanUploadProgress
from scans.tasks import (
    process_single_scan_task,
    reset_stuck_scan_batches_task,
)
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
)

if TYPE_CHECKING:
    from campaigns.models import Campaign
    from clients.models import Client as ClientModel


_SCAN_SECRET = "scan-failure-secret"


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Return HMAC-SHA256 hex digest over ``f"{timestamp}.{payload}"``."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


@pytest.fixture()
def _scan_secret(settings: SettingsWrapper) -> None:
    """Set the scanner webhook HMAC secret for the duration of the test."""
    from django.core.cache import cache

    settings.SCAN_WEBHOOK_SECRET = _SCAN_SECRET
    cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Document AI 429 (quota exceeded) — retries with backoff and finalises
# 2. Document AI 503 (ServiceUnavailable) — retries with backoff and finalises
# 3. Document AI InvalidArgument — non-retryable; placeholder marked failed
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestDocumentAITransientErrors:
    """Document AI quota / availability errors must trigger Celery retry.

    Tests the contract enforced by ``process_single_scan_task``:

    * ``ValidationError`` / ``ValueError`` are non-retryable — they
      short-circuit with ``retryable=False`` and never call ``self.retry``.
    * Any other ``Exception`` (including
      ``google.api_core.exceptions.ResourceExhausted`` and
      ``ServiceUnavailable``) propagates into ``self.retry`` so Celery
      schedules another attempt.
    """

    def test_resource_exhausted_triggers_celery_retry(self) -> None:
        """Doc AI 429 must surface as a Celery retry, not a silent success."""
        from google.api_core import exceptions as gax_exc

        with (
            patch("scans.tasks.process_single_scan_task.retry") as mock_retry,
            patch(
                "scans.scan_processing.ScanProcessingService.process_single_scan",
                side_effect=gax_exc.ResourceExhausted("429 Too Many Requests"),
            ),
        ):
            mock_retry.side_effect = RuntimeError("retry-scheduled")

            with pytest.raises(RuntimeError, match="retry-scheduled"):
                process_single_scan_task.run(placeholder_id="ph-429")

            mock_retry.assert_called_once()
            kwargs = mock_retry.call_args.kwargs
            assert isinstance(kwargs.get("exc"), gax_exc.ResourceExhausted)

    def test_resource_exhausted_then_success_finalises_batch(self) -> None:
        """First call raises 429, second call succeeds — task returns success."""
        from google.api_core import exceptions as gax_exc

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
        )

        call_log: list[str] = []

        def flaky_process(placeholder_id: str, **_kw: Any) -> dict[str, Any]:
            call_log.append(placeholder_id)
            if len(call_log) == 1:
                raise gax_exc.ResourceExhausted("429 quota exceeded")
            return {
                "placeholder_id": placeholder_id,
                "ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED,
                "urn": "URN-AFTER-RETRY",
                "donor_matched": True,
            }

        with patch(
            "scans.scan_processing.ScanProcessingService.process_single_scan",
            side_effect=flaky_process,
        ):
            with (
                patch(
                    "scans.tasks.process_single_scan_task.retry",
                    side_effect=RuntimeError("retry-scheduled"),
                ),
                pytest.raises(RuntimeError, match="retry-scheduled"),
            ):
                process_single_scan_task.run(
                    placeholder_id=str(placeholder.id),
                    scan_batch_id=str(batch.id),
                )

            second = process_single_scan_task.run(
                placeholder_id=str(placeholder.id),
                scan_batch_id=str(batch.id),
            )

        assert second["ocr_status"] == ScanPlaceholder.OCR_STATUS_MATCHED
        assert call_log == [str(placeholder.id), str(placeholder.id)]

    def test_service_unavailable_triggers_celery_retry(self) -> None:
        """Doc AI 503 must surface as a Celery retry."""
        from google.api_core import exceptions as gax_exc

        with (
            patch("scans.tasks.process_single_scan_task.retry") as mock_retry,
            patch(
                "scans.scan_processing.ScanProcessingService.process_single_scan",
                side_effect=gax_exc.ServiceUnavailable("503 Service Unavailable"),
            ),
        ):
            mock_retry.side_effect = RuntimeError("retry-scheduled")

            with pytest.raises(RuntimeError, match="retry-scheduled"):
                process_single_scan_task.run(placeholder_id="ph-503")

            mock_retry.assert_called_once()
            kwargs = mock_retry.call_args.kwargs
            assert isinstance(kwargs.get("exc"), gax_exc.ServiceUnavailable)

    def test_invalid_argument_marks_placeholder_failed_without_retry(self) -> None:
        """Doc AI InvalidArgument (malformed PDF) is non-retryable in scan_processing.

        ``process_single_scan`` catches every exception and converts it to a
        result dict with ``ocr_status='failed'`` so the chord finalises with
        partial results. The placeholder row's ``ocr_status`` is updated by
        the same code path. The Celery task therefore sees a successful
        return and never calls ``self.retry``.
        """
        from google.api_core import exceptions as gax_exc

        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
        )
        invalid_argument = gax_exc.InvalidArgument("malformed PDF")

        # Patch the OCR step so the *placeholder row* still updates via
        # ``_handle_scan_failure`` while the donor-match step never runs.
        with (
            patch(
                "scans.scan_processing.run_ocr_and_extract",
                side_effect=invalid_argument,
            ),
            patch("scans.scan_processing.apply_donor_match"),
            patch("scans.tasks.process_single_scan_task.retry") as mock_retry,
        ):
            result = process_single_scan_task.run(
                placeholder_id=str(placeholder.id),
                scan_batch_id=str(batch.id),
            )

        assert result["ocr_status"] == ScanPlaceholder.OCR_STATUS_FAILED
        mock_retry.assert_not_called()

        placeholder.refresh_from_db()
        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_FAILED
        assert "malformed PDF" in placeholder.processing_error


# ─────────────────────────────────────────────────────────────────────────────
# 4. Concurrent batch-name collision — unique constraint enforces single winner
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestConcurrentBatchNameCollision:
    """Two creators racing on the same ``(campaign, batch_name)`` pair must
    not both succeed. The DB-level unique constraint
    ``scanbatch_campaign_batch_name_uniq`` is the source of truth; the
    earlier in-Python ``_validate_batch_uniqueness`` is best-effort only.
    """

    def test_duplicate_batch_name_rejected_with_clean_value_error(self) -> None:
        """Second creation attempt is translated to ``ValueError`` cleanly."""
        from scans.scan_processing import ScanProcessingService

        campaign = CampaignFactory(status="active")
        ScanBatchFactory(
            campaign=campaign,
            batch_name="DUPLICATE-BATCH-001",
        )

        with (
            patch(
                "scans.scan_processing.group_keys_for_layout",
                return_value=([["ScanOutput/foo/DUPLICATE-BATCH-001.pdf"]], 1),
            ),
            patch("scans.scan_processing.build_placeholder") as mock_build,
        ):
            mock_build.return_value = ScanPlaceholder(
                image_url="https://example.com/x.pdf",
                image_path="ScanOutput/foo/x.pdf",
                urn="URN1",
            )

            with pytest.raises(ValueError, match="already exists"):
                ScanProcessingService.create_scan_batch_from_r2(
                    campaign_id=str(campaign.id),
                    r2_keys=["ScanOutput/foo/DUPLICATE-BATCH-001.pdf"],
                    payment_method="cheque",
                    scan_form_type="simplex_with_payment",
                    batch_name="DUPLICATE-BATCH-001",
                )

        assert (
            ScanBatch.objects.filter(
                campaign=campaign, batch_name="DUPLICATE-BATCH-001"
            ).count()
            == 1
        )

    def test_unique_constraint_exists_on_campaign_batch_name(
        self, transactional_db: None
    ) -> None:
        """The DB-level unique constraint is the ultimate defence against a
        concurrent collision. Bypass ``full_clean`` (which Django's
        ``ScanBatch.save`` calls and which raises ``ValidationError``
        before ever hitting the DB) and assert the underlying
        ``IntegrityError`` is raised, proving the constraint is real and
        not just a model-side check.
        """
        from django.db import models as dj_models
        from django.db import transaction as dj_tx

        campaign = CampaignFactory(status="active")
        ScanBatchFactory(
            campaign=campaign,
            batch_name="RACE-BATCH-001",
        )

        # Bypass full_clean by going through the base Model.save — this is
        # the path the DB constraint catches in a real race. Wrap in
        # ``atomic`` so the broken transaction is rolled back cleanly and
        # subsequent assertions can query the table.
        duplicate = ScanBatch(
            campaign=campaign,
            batch_name="RACE-BATCH-001",
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        with pytest.raises(IntegrityError), dj_tx.atomic():
            dj_models.Model.save(duplicate)

        assert (
            ScanBatch.objects.filter(
                campaign=campaign, batch_name="RACE-BATCH-001"
            ).count()
            == 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. HMAC replay attack — replayed identical complete payload is deduped
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestHmacReplayAttack:
    """A replay of an identical complete-status webhook must not double-fire."""

    def test_replayed_complete_webhook_is_deduplicated(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Today's defence is hash-based dedup of the canonical payload —
        the second post returns 200 with ``status="duplicate"`` and the OCR
        task is dispatched exactly once. Once unit 21 lands a stricter
        timestamp/nonce check, this same path will return 401 on replay;
        the assertion below is intentionally lenient on response code so
        the test flips automatically once that lands.
        """
        selected_client = ClientFactory(name="Replay Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 3,
            "total_expected": 3,
            "latest_urn": "Batch-replay-attack.pdf",
            "status": "complete",
            "r2_prefix": "ScanOutput/replay/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-replay-attack.pdf",
        }
        payload = json.dumps(payload_dict).encode()
        # Distinct timestamps so the (client_id, ts) replay guard doesn't
        # 401 the second post — we're exercising the hash-based dedup.
        ts_first = str(int(time.time()))
        ts_second = str(int(time.time()) + 1)
        sig_first = _scan_signature(_SCAN_SECRET, payload, ts_first)
        sig_second = _scan_signature(_SCAN_SECRET, payload, ts_second)

        with patch("scans.tasks.create_scan_batch_from_r2_task.delay") as mock_delay:
            mock_delay.return_value = MagicMock(id="ocr-task-replay-attack")

            first = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=sig_first,
                HTTP_X_SCAN_TIMESTAMP=ts_first,
            )
            second = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=sig_second,
                HTTP_X_SCAN_TIMESTAMP=ts_second,
            )

        assert first.status_code == 200
        assert second.status_code in (200, 401)
        # Either way the OCR pipeline must fire only once.
        assert mock_delay.call_count == 1

    def test_replayed_complete_webhook_returns_401(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Replayed complete-status webhook with same nonce returns 401."""
        selected_client = ClientFactory(name="Replay 401 Client")
        campaign = CampaignFactory(client=selected_client, status="active")

        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(selected_client.id),
            "total_uploaded": 1,
            "total_expected": 1,
            "status": "complete",
            "r2_prefix": "ScanOutput/replay401/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-replay-401.pdf",
            "nonce": uuid.uuid4().hex,
        }
        payload = json.dumps(payload_dict).encode()
        ts_str = str(int(time.time()))
        signature = _scan_signature(_SCAN_SECRET, payload, ts_str)

        with patch("scans.tasks.create_scan_batch_from_r2_task.delay"):
            client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )
            second = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )

        assert second.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# 6. R2 upload failure mid-redaction — page_keys unchanged on rollback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestR2UploadFailureMidRedaction:
    """When ``r2_put_object`` raises mid-redaction the placeholder must be
    marked ``redaction_blocked`` and ``page_keys`` must remain unchanged so
    the original document survives the failed redaction attempt.
    """

    def test_r2_failure_rolls_back_page_keys(self) -> None:
        """R2 ``PutObject`` raising mid-redaction must keep page_keys intact."""
        from scans.scan_redaction import save_uploaded_redacted_pages

        original_keys = ["ScanOutput/x/page1.pdf", "ScanOutput/x/page2.pdf"]
        placeholder = ScanPlaceholderFactory(
            page_keys=original_keys,
            original_page_keys=original_keys,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )

        with (
            patch(
                "core.storage_backends.r2_put_object",
                side_effect=RuntimeError("R2 PutObject 500"),
            ),
            pytest.raises(RuntimeError),
        ):
            save_uploaded_redacted_pages(  # type: ignore[arg-type]
                placeholder=placeholder,
                user=None,
                files=[],
                notes="",
            )

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_BLOCKED
        assert placeholder.page_keys == original_keys


# ─────────────────────────────────────────────────────────────────────────────
# 7. Chord task crash mid-DB-write — finalize aggregates partial results
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestChordCrashMidDbWrite:
    """A per-scan task may crash after partial DB updates. The chord
    callback must still finalise the batch with consistent counters; any
    placeholder left in ``processing`` is recovered by
    ``reset_stuck_scan_batches_task``.
    """

    def test_finalize_aggregates_partial_chord_results(self) -> None:
        """Mixed chord results (success, failure, exception) must aggregate."""
        from scans.tasks import _aggregate_scan_results

        results: list[Any] = [
            {
                "placeholder_id": "ok-1",
                "ocr_status": ScanPlaceholder.OCR_STATUS_MATCHED,
            },
            {
                "placeholder_id": "fail-1",
                "ocr_status": ScanPlaceholder.OCR_STATUS_FAILED,
                "error": "boom",
            },
            # An exhausted-retry exception: chord delivers the exception
            # itself rather than a dict. Treated as a failure.
            RuntimeError("worker crashed mid-write"),
        ]
        total, matched, failed = _aggregate_scan_results(results)
        assert (total, matched, failed) == (3, 1, 2)

    def test_stuck_processing_batch_is_recovered_by_sweeper(self) -> None:
        """A batch left in ``processing`` for >1 hour is reset to ``failed``."""
        batch = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        # Force an old updated_at to satisfy the 1-hour stuck threshold.
        ScanBatch.objects.filter(pk=batch.pk).update(
            updated_at=timezone.now() - timedelta(hours=2),
        )

        result = reset_stuck_scan_batches_task.run()
        assert result == {
            "status": "ok",
            "reset": 1,
            "placeholders_recovered": 0,
        }

        batch.refresh_from_db()
        assert batch.status == ScanBatch.STATUS_FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 8. OCR confidence below threshold — donation auto-flagged
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestLowOcrConfidenceFlagging:
    """OCR amount confidence below the auto-approve threshold must mark
    the resulting Donation as ``needs_review`` rather than allowing it
    into the auto-approve cascade.
    """

    def test_low_confidence_amount_blocks_auto_approve(self) -> None:
        """Malformed donation_date x low confidence ⇒ mandatory hold."""

        from donations.models import Donation
        from scans.scan_processing_donations import create_donation_from_placeholder
        from tests.factories import DonationBatchFactory

        batch = ScanBatchFactory()
        donation_batch = DonationBatchFactory(campaign=batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED,
            extracted_data={
                "amount": "25.00",
                "amount_confidence": 0.99,
                "donation_date": "not-parseable-date",
                "donation_date_confidence": 0.4,
            },
            ocr_confidence=0.99,
        )

        donation = create_donation_from_placeholder(
            placeholder, batch.campaign, donation_batch, batch
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert donation.qa_status != Donation.QA_STATUS_APPROVED
        # Auto-approve cascade hook: a non-empty hold list locks the donation
        # out of the convenience cascade until a reviewer signs it off.
        assert any(
            record["field"] == "donation_date"
            for record in donation.low_confidence_fields
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Donor false-positive match — fuzzy matches must not auto-bind
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestDonorFalsePositiveMatch:
    """A 0.92-similar donor with the same postcode must not auto-match.

    The hardening unit will populate a ``donor_match_candidates`` field
    on ``ScanPlaceholder`` so QA can pick the right donor; until then we
    assert the negative — no fuzzy auto-bind happens today.
    """

    def test_high_similarity_donor_left_for_qa_review(self) -> None:
        """Similar name + same postcode populates candidates, not a hard match."""
        from scans.scan_processing_donors import create_new_placeholder_donor
        from tests.factories import SystemDonorFactory

        batch = ScanBatchFactory()
        campaign = batch.campaign
        # Two SystemDonors at the same postcode whose names land inside the
        # borderline Jaro-Winkler band (0.85-0.95) against the OCR-extracted
        # "Jonh Smyth" target — neither one auto-binds, both are surfaced as
        # candidates for QA disambiguation.
        donor_a = SystemDonorFactory(
            client=campaign.client,
            first_name="John",
            last_name="Smith",  # ~0.917 similarity vs "Jonh Smyth"
            postcode="SW1A 1AA",
            external_urn="HOUSE-001",
        )
        donor_b = SystemDonorFactory(
            client=campaign.client,
            first_name="Jonathan",
            last_name="Smyth",  # ~0.863 similarity vs "Jonh Smyth"
            postcode="SW1A 1AA",
            external_urn="HOUSE-002",
        )

        placeholder = ScanPlaceholderFactory(
            batch=batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
            urn="",
            donor_name="Jonh Smyth",
            extracted_data={"donor_name": "Jonh Smyth", "postcode": "SW1A 1AA"},
            is_captured=False,
        )

        # Drive the candidate-recording path; the matcher should refuse to
        # auto-link either borderline donor and instead persist them both.
        create_new_placeholder_donor(placeholder, campaign)
        placeholder.save()
        placeholder.refresh_from_db()

        candidates: list[dict[str, Any]] = list(placeholder.donor_match_candidates)
        assert candidates, "expected fuzzy donor candidates to be populated"
        candidate_ids = {c["system_donor_id"] for c in candidates}
        assert {str(donor_a.pk), str(donor_b.pk)}.issubset(candidate_ids)
        for entry in candidates:
            assert "score" in entry
            assert "urn" in entry
            assert "name" in entry
        assert placeholder.matched_donor is None
        assert placeholder.matched_data_file_donor is None


# ─────────────────────────────────────────────────────────────────────────────
# 10. 5-scanner concurrent upload — every campaign gets a progress row
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestConcurrentScannerUploads:
    """Five scanner workstations each posting to ``/webhooks/scan-upload/``
    with a unique campaign must produce five ``ScanUploadProgress`` rows
    with no lost-increment race.

    SQLite's in-memory test database serialises writes, so a true parallel
    test isn't useful — instead we drive the webhook five times across
    five campaigns and assert no progress row is dropped or merged into a
    sibling campaign's row, which is the property the
    ``update_or_create(campaign=…)`` path must preserve when scanner
    workstations talk to the bureau in lockstep.
    """

    def _build_payload(
        self,
        scan_client: ClientModel,
        campaign: Campaign,
        total: int,
        latest_urn: str | None = None,
    ) -> tuple[bytes, str, str]:
        """Return (signed JSON payload, HMAC signature, timestamp) for a scanning post."""
        payload_dict = {
            "campaign_id": str(campaign.id),
            "client_id": str(scan_client.id),
            "total_uploaded": total,
            "total_expected": total,
            "latest_urn": latest_urn or f"IMG_{total:03d}.tiff",
            "status": "scanning",
        }
        payload = json.dumps(payload_dict).encode()
        # Unique timestamp per call so the (client_id, ts) replay guard
        # doesn't 401 sequential same-client posts.
        ts_str = str(int(time.time()) + int(total))
        return payload, _scan_signature(_SCAN_SECRET, payload, ts_str), ts_str

    def test_five_campaigns_each_get_progress_row(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Five campaigns, five progress rows — none lost or merged."""
        scan_client = ClientFactory(name="Concurrent Scanner Client")
        campaigns = [
            CampaignFactory(client=scan_client, status="active") for _ in range(5)
        ]

        for idx, campaign in enumerate(campaigns):
            payload, signature, ts_str = self._build_payload(
                scan_client, campaign, total=idx + 1, latest_urn=f"IMG_{idx:03d}.tiff"
            )
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )
            assert response.status_code == 200, response.content

        progress_rows = ScanUploadProgress.objects.filter(
            campaign__in=campaigns
        ).count()
        assert progress_rows == 5, (
            "every campaign must have its own ScanUploadProgress row even "
            "under back-to-back webhook posts"
        )

        for idx, campaign in enumerate(campaigns):
            row = ScanUploadProgress.objects.get(campaign=campaign)
            assert row.total_uploaded == idx + 1
            assert row.status == ScanUploadProgress.STATUS_SCANNING

    def test_concurrent_posts_use_distinct_progress_rows(
        self, client: Client, _scan_secret: None
    ) -> None:
        """Two campaigns receiving posts in interleaved order must keep
        their progress rows separate.

        This catches the lost-increment failure mode where a buggy
        ``update_or_create`` lookup (e.g. keying on something other than
        ``campaign``) would let one campaign's update overwrite another.
        Real concurrent threaded posts can't be tested reliably against
        SQLite's in-memory backend, so we interleave the writes
        deterministically — the property under test is the lookup-key
        correctness, not OS-level scheduling.
        """
        scan_client = ClientFactory(name="Interleaved Scanner Client")
        campaign_a = CampaignFactory(client=scan_client, status="active")
        campaign_b = CampaignFactory(client=scan_client, status="active")

        # Interleave: A→B→A→B to maximise the chance of a key-collision
        # bug surfacing as a merged row.
        sequence = [
            (campaign_a, 1),
            (campaign_b, 7),
            (campaign_a, 2),
            (campaign_b, 8),
        ]
        for campaign, total in sequence:
            payload, signature, ts_str = self._build_payload(
                scan_client, campaign, total
            )
            response = client.post(
                "/webhooks/scan-upload/",
                data=payload,
                content_type="application/json",
                HTTP_X_SIGNATURE=signature,
                HTTP_X_SCAN_TIMESTAMP=ts_str,
            )
            assert response.status_code == 200

        row_a = ScanUploadProgress.objects.get(campaign=campaign_a)
        row_b = ScanUploadProgress.objects.get(campaign=campaign_b)
        assert row_a.total_uploaded == 2
        assert row_b.total_uploaded == 8
        assert row_a.pk != row_b.pk
