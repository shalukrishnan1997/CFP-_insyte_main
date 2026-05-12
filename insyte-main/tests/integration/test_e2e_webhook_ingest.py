"""End-to-end happy-path test for the scan-upload webhook ingest pipeline.

Drives the production webhook entry point ``/webhooks/scan-upload/`` through
to ``ScanBatch`` / ``ScanPlaceholder`` / ``DonationBatch`` creation using
a 4-donor minibatch sliced from the ``000015.pdf`` Kodak fixture. R2 listing
and Document AI are stubbed so the test exercises real Django + Celery
plumbing without external dependencies.

The pipeline under test:

1. Scanner workstation POSTs an HMAC-signed ``status="complete"`` payload.
2. ``core.webhooks.scan_upload_webhook`` validates HMAC + replay protection.
3. ``core.webhooks._handle_upload_complete`` queues
   ``scans.tasks.create_scan_batch_from_r2_task`` (eager in tests).
4. The task lists R2 (stubbed) and creates the ``ScanBatch`` +
   per-donor ``ScanPlaceholder`` rows.
5. ``process_scan_batch_task`` fan-outs OCR (we drive each placeholder
   directly to keep the assertions deterministic — chord callbacks behave
   poorly under ``CELERY_TASK_ALWAYS_EAGER``).
6. ``finalize_scan_batch_task`` creates the ``DonationBatch``.
"""

import hashlib
import hmac
import io
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pypdfium2
import pytest
from django.core.cache import cache
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonorFactory,
    UserFactory,
)

# Path to the staged 64-page Kodak donation form fixture. The test uses
# only the first four pages to keep the run fast.
FIXTURE_PDF: Path = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"

# Webhook secret used by every test in this module.
SCAN_SECRET = "scan-secret-e2e-ingest"

# Number of donors in the minibatch we drive end-to-end. Four is the smallest
# sample that still exercises the multi-placeholder fan-out without making
# the test slow.
DONOR_COUNT = 4

R2_PREFIX = "ScanOutput/test-charity/spring25/cash/"


def _scan_signature(secret: str, payload: bytes, timestamp: str) -> str:
    """Compute the HMAC-SHA256 signature the production helper expects."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _post_complete_webhook(
    client: Client,
    payload_dict: dict[str, Any],
    *,
    secret: str = SCAN_SECRET,
    timestamp: int | None = None,
) -> Any:
    """Sign and POST a ``status="complete"`` payload to the scan-upload webhook."""
    payload = json.dumps(payload_dict).encode()
    ts_str = str(timestamp if timestamp is not None else int(time.time()))
    signature = _scan_signature(secret, payload, ts_str)
    return client.post(
        "/webhooks/scan-upload/",
        data=payload,
        content_type="application/json",
        HTTP_X_SIGNATURE=signature,
        HTTP_X_SCAN_TIMESTAMP=ts_str,
    )


def _slice_pdf_to_jpeg_bytes(pdf_path: Path, page_count: int) -> list[bytes]:
    """Render the first ``page_count`` pages of ``pdf_path`` to JPEG bytes.

    The bytes are not used for actual OCR (we stub that), but rendering them
    confirms the fixture parses cleanly under pypdfium2 and serves as the
    source artefact a real scanner workstation would upload.
    """
    pdf = pypdfium2.PdfDocument(str(pdf_path))
    page_bytes: list[bytes] = []
    try:
        for page_index in range(min(page_count, len(pdf))):
            page = pdf[page_index]
            bitmap = page.render(scale=1).to_pil()
            buffer = io.BytesIO()
            bitmap.save(buffer, format="JPEG")
            page_bytes.append(buffer.getvalue())
    finally:
        pdf.close()
    return page_bytes


def _build_source_pdf_key(batch_filename: str) -> str:
    """Return the canonical R2 key the scanner workstation would have uploaded.

    The physical-batch workflow uploads exactly one PDF; the ingest pipeline
    splits it into per-page objects later. ``r2_list_prefix`` therefore
    returns the single PDF key.
    """
    return f"{R2_PREFIX}{batch_filename}"


def _build_split_page_keys(urns: list[str]) -> list[str]:
    """Return the per-donor split-page keys ``expand_pdf_keys_for_campaign``
    would produce for the minibatch.

    Real R2 split keys live under ``<prefix>/split/<base>_doc_<NNNN>.png``,
    but the canonical URN is recovered from the *filename* via
    ``extract_urn_from_key``. We therefore use ``<prefix>/split/<URN>.png``
    so each placeholder lands with the matching donor's URN already set.
    """
    return [f"{R2_PREFIX}split/{urn}.png" for urn in urns]


def _fake_run_ocr_and_extract(
    placeholder: Any,
    _campaign: Any,
    _known_urns: list[str] | None,
) -> dict[str, Any]:
    """Stand-in for Document AI extraction.

    ``build_placeholder`` already extracted the URN from the R2 key filename,
    so the matched donor lookup will succeed without us touching
    ``placeholder.urn``. We just need to populate the OCR metadata that
    ``apply_donor_match`` reads.
    """
    placeholder.ocr_data = {"qr_kind": "", "total_pages_processed": 1}
    placeholder.qr_decoded = False
    placeholder.ocr_confidence = 0.95
    placeholder.extracted_data = {}
    return {}


def _drive_chord_eagerly(
    scan_batch_id: str, known_urns: list[str], placeholder_ids: list[str]
) -> None:
    """Replacement for ``_dispatch_scan_chord`` that runs synchronously.

    Celery's chord primitive cannot be relied on under
    ``CELERY_TASK_ALWAYS_EAGER`` — the body sometimes runs with the wrong
    argument list. We instead invoke each ``process_single_scan_task`` and
    ``finalize_scan_batch_task`` directly so the test asserts on the real
    DB transitions without race risk.
    """
    from scans.tasks import finalize_scan_batch_task, process_single_scan_task

    results: list[dict[str, Any]] = []
    for placeholder_id in placeholder_ids:
        result = process_single_scan_task.run(placeholder_id, scan_batch_id, known_urns)
        results.append(result)
    finalize_scan_batch_task.run(results, scan_batch_id)


@pytest.fixture()
def webhook_settings(settings: SettingsWrapper) -> Iterator[SettingsWrapper]:
    """Pin webhook auth settings and reset replay-protection cache state."""
    settings.SCAN_WEBHOOK_SECRET = SCAN_SECRET
    settings.SCAN_WEBHOOK_TIMESTAMP_REQUIRED = False
    cache.clear()
    yield settings
    cache.clear()


@pytest.fixture()
def four_page_minibatch() -> list[bytes]:
    """Return JPEG bytes for the first four pages of ``000015.pdf``.

    The bytes are not consumed by the production code path (OCR is stubbed),
    but their successful generation proves the staged PDF fixture is intact.
    """
    assert FIXTURE_PDF.exists(), (
        f"Missing fixture: {FIXTURE_PDF}. Stage it before running this test."
    )
    pages = _slice_pdf_to_jpeg_bytes(FIXTURE_PDF, DONOR_COUNT)
    assert len(pages) == DONOR_COUNT, f"Expected {DONOR_COUNT} pages, got {len(pages)}"
    return pages


@pytest.mark.django_db()
class TestWebhookIngestHappyPath:
    """End-to-end happy path through the scan-upload webhook."""

    def test_complete_webhook_creates_scan_batch_placeholders_and_donation_batch(
        self,
        client: Client,
        webhook_settings: SettingsWrapper,
        four_page_minibatch: list[bytes],
    ) -> None:
        """The full ingest pipeline produces the expected DB state.

        Asserts:
            * Webhook returns 200 with an ``ocr_task_id``.
            * Exactly one ``ScanBatch`` is created with ``total_scans=4``.
            * ``ScanPlaceholder`` rows are created one per donor (4 total),
              all transition to ``OCR_STATUS_MATCHED`` because each URN maps
              to a pre-seeded ``Donor``.
            * Exactly one ``DonationBatch`` is created with 4 donations.
        """
        from donations.models import Donation, DonationBatch
        from scans.models import ScanBatch, ScanPlaceholder

        # Sanity check the fixture rendered to four real JPEG payloads.
        assert all(payload[:3] == b"\xff\xd8\xff" for payload in four_page_minibatch)

        creator = UserFactory()
        charity = ClientFactory(name="E2E Test Charity")
        campaign = CampaignFactory(
            client=charity,
            status="active",
            campaign_temperature="cold",
            scan_purpose="donation",
            donor_source="house_file",
            created_by=creator,
        )

        # Pre-seed donors whose URNs the OCR stub will set on each placeholder.
        urns = [f"E2EURN{i:04d}" for i in range(DONOR_COUNT)]
        for urn in urns:
            DonorFactory(urn=urn, client=charity, created_by=creator)

        batch_filename = f"e2e-{uuid.uuid4().hex[:8]}.pdf"
        source_pdf_key = _build_source_pdf_key(batch_filename)
        split_keys = _build_split_page_keys(urns)

        with (
            patch(
                "core.storage_backends.r2_list_prefix",
                return_value=[source_pdf_key],
            ),
            # ``expand_pdf_keys_for_campaign`` would normally download the
            # source PDF from R2 and split it page by page. R2 isn't wired in
            # tests, so we substitute the result directly.
            patch(
                "scans.scan_processing_r2.expand_pdf_keys_for_campaign",
                return_value=split_keys,
            ),
            patch(
                "scans.scan_processing.run_ocr_and_extract",
                side_effect=_fake_run_ocr_and_extract,
            ),
            patch(
                "scans.tasks._dispatch_scan_chord",
                side_effect=_drive_chord_eagerly,
            ),
        ):
            payload = {
                "campaign_id": str(campaign.id),
                "client_id": str(charity.id),
                "total_uploaded": DONOR_COUNT,
                "total_expected": DONOR_COUNT,
                "latest_urn": batch_filename,
                "status": "complete",
                "r2_prefix": R2_PREFIX,
                "payment_method": "cash",
                "scan_form_type": "simplex",
                "batch_name": batch_filename,
                "auto_process": True,
            }
            response = _post_complete_webhook(client, payload)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["ok"] is True
        assert body["total_uploaded"] == DONOR_COUNT
        assert body.get("ocr_task_id"), "Expected webhook to return an OCR task id"

        scan_batches = ScanBatch.objects.filter(campaign=campaign)
        assert scan_batches.count() == 1
        scan_batch = scan_batches.get()
        assert scan_batch.batch_name == batch_filename.removesuffix(".pdf")
        assert scan_batch.payment_method == "cash"
        assert scan_batch.scan_form_type == "simplex"
        assert scan_batch.total_scans == DONOR_COUNT
        assert scan_batch.status == ScanBatch.STATUS_COMPLETED
        assert scan_batch.processed_scans == DONOR_COUNT
        assert scan_batch.matched_scans == DONOR_COUNT

        placeholders = ScanPlaceholder.objects.filter(batch=scan_batch).order_by("urn")
        assert placeholders.count() == DONOR_COUNT
        assert sorted(p.urn for p in placeholders) == sorted(urns)
        assert all(
            p.ocr_status == ScanPlaceholder.OCR_STATUS_MATCHED for p in placeholders
        ), [(p.urn, p.ocr_status) for p in placeholders]
        assert all(p.matched_donor is not None for p in placeholders)

        donation_batches = DonationBatch.objects.filter(campaign=campaign)
        assert donation_batches.count() == 1
        donation_batch = donation_batches.get()
        assert donation_batch.batch_name == scan_batch.batch_name
        assert donation_batch.default_payment_method == "cash"
        assert donation_batch.status == DonationBatch.STATUS_PENDING_QA
        assert donation_batch.total_donations == DONOR_COUNT

        donations = Donation.objects.filter(batch=donation_batch)
        assert donations.count() == DONOR_COUNT
        assert {d.donor.urn for d in donations} == set(urns)

        # The ScanBatch is wired to the freshly-created DonationBatch so QA
        # operators can navigate from the scan view straight to the donations.
        scan_batch.refresh_from_db()
        assert scan_batch.donation_batch_id == donation_batch.id
