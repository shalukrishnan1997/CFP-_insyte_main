"""End-to-end integration tests for OCR low-confidence QA flagging.

Covers the full ingest → donation-creation → batch-approval flow for
placeholders whose OCR amount/date confidence falls below the per-campaign
threshold. The contract under test:

* ``ScanProcessingService`` populates ``ScanPlaceholder.extracted_data`` with
  per-field confidence scores from the (stubbed) Document AI extractor.
* ``create_donation_from_placeholder`` flags the resulting Donation with
  ``QA_STATUS_FLAGGED`` and records ``low_confidence_fields`` so the QA UI
  can surface which scores need human verification.
* The ``qa_approve_batch`` cascade auto-approves clean donations but leaves
  donations with a populated ``low_confidence_fields`` list flagged for
  reviewer sign-off.

Document AI and R2 storage are stubbed; only the database state and view
side-effects are exercised so the suite stays hermetic. The PDF fixture
(``tests/fixtures/000015.pdf``) is staged as the canonical scanner output
for this scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from clients.models import Client as ClientModel
from donations.models import Donation, DonationBatch
from donors.models import Donor, SystemDonor
from scans.models import ScanBatch, ScanPlaceholder
from scans.scan_processing_donations import (
    create_donation_batch,
    create_donation_from_placeholder,
)
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    SystemDonorFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PipelineSetup:
    """Shared scaffolding for an OCR-low-confidence pipeline test.

    Attributes:
        client: Charity owning the donor and campaign.
        donor: House-file donor used to satisfy donor-match metadata.
        system_donor: Internal system-of-record donor.
        scan_batch: Parent scan batch the placeholders belong to.
        donation_batch: Pre-created DonationBatch for direct-call tests.
    """

    client: ClientModel
    donor: Donor
    system_donor: SystemDonor
    scan_batch: ScanBatch
    donation_batch: DonationBatch


def _build_pipeline_setup(threshold: Decimal) -> _PipelineSetup:
    """Build a matched-donor scan-batch scaffolding for one test.

    The factories' ``client`` foreign keys are nullable, so callers must
    seed an explicit ``Client`` and pass it through every related row to
    avoid a ``NOT NULL`` collision on the campaigns table.
    """
    scan_client = ClientFactory()
    donor = DonorFactory(client=scan_client)
    system_donor = SystemDonorFactory(client=scan_client)
    campaign = CampaignFactory(
        client=scan_client,
        ocr_confidence_threshold=threshold,
    )
    scan_batch = ScanBatchFactory(campaign=campaign, payment_method="cheque")
    donation_batch = DonationBatchFactory(campaign=campaign)
    return _PipelineSetup(
        client=scan_client,
        donor=donor,
        system_donor=system_donor,
        scan_batch=scan_batch,
        donation_batch=donation_batch,
    )


def _stub_extracted_payload(
    *,
    amount: str = "100.00",
    amount_confidence: float = 0.6,
    donation_date_confidence: float = 0.95,
    urn_confidence: float = 0.95,
    payment_method_confidence: float = 0.95,
) -> dict[str, Any]:
    """Return a Document-AI-shaped extracted-data dict with per-field scores.

    When the amount confidence is below the organisational threshold AND the
    amount itself is absent or malformed, callers should expect the mandatory
    hold path to fire once per-field scores are attached.
    """
    return {
        "amount": amount,
        "cheque_date": "01/05/2026",
        "amount_confidence": amount_confidence,
        "donation_date_confidence": donation_date_confidence,
        "urn_confidence": urn_confidence,
        "payment_method_confidence": payment_method_confidence,
    }


def _build_placeholder(
    setup: _PipelineSetup,
    extracted: dict[str, Any],
) -> ScanPlaceholder:
    """Create a matched ScanPlaceholder pre-populated with stubbed OCR data."""
    return ScanPlaceholderFactory(
        batch=setup.scan_batch,
        matched_donor=setup.donor,
        matched_system_donor=setup.system_donor,
        ocr_confidence=0.99,
        ocr_data={"donor_match_status": "matched"},
        extracted_data=extracted,
        ocr_status=ScanPlaceholder.OCR_STATUS_MATCHED,
    )


# ---------------------------------------------------------------------------
# Fixture sanity check
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestPdfFixtureStaged:
    """The scanner-output PDF fixture must be present for downstream stubs."""

    def test_fixture_pdf_is_present(self) -> None:
        """``tests/fixtures/000015.pdf`` is the canonical scanner sample."""
        assert FIXTURE_PDF.exists(), (
            "Expected the scanner-output sample PDF at tests/fixtures/000015.pdf"
        )
        assert FIXTURE_PDF.stat().st_size > 0


# ---------------------------------------------------------------------------
# E2E: low-confidence amount must flag the Donation for QA review
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestE2eLowConfidenceFlagsDonationForQa:
    """Drive the donation-creation pipeline with a stubbed low-confidence OCR read."""

    def test_low_confidence_amount_flags_donation_for_qa(self) -> None:
        """Stub blank amount confidence 0.6 < threshold ⇒ mandatory hold."""

        setup = _build_pipeline_setup(threshold=Decimal("0.850"))
        extracted = _stub_extracted_payload(amount="", amount_confidence=0.6)

        # Stub Document AI: the OCR layer would otherwise hit Google.
        with patch(
            "scans.scan_processing_ocr.run_ocr_and_extract",
            return_value=extracted,
        ):
            placeholder = _build_placeholder(setup, extracted)
            donation = create_donation_from_placeholder(
                placeholder,
                setup.scan_batch.campaign,
                setup.donation_batch,
                setup.scan_batch,
            )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert donation.qa_status != Donation.QA_STATUS_APPROVED
        # Hold record contains the offending field + the score the OCR scraped.
        records = donation.low_confidence_fields
        assert any(record["field"] == "amount" for record in records), (
            "Expected the amount field to be recorded as low-confidence"
        )
        amount_record = next(r for r in records if r["field"] == "amount")
        assert pytest.approx(float(amount_record["confidence"]), abs=1e-3) == 0.6

    def test_structurally_sound_amount_keeps_hold_empty_under_threshold_gap(
        self,
    ) -> None:
        """Even when OCR score is noisy, coherent amounts must not deadlock QA."""
        setup = _build_pipeline_setup(threshold=Decimal("0.850"))
        extracted = _stub_extracted_payload(amount="125.40", amount_confidence=0.6)

        with patch(
            "scans.scan_processing_ocr.run_ocr_and_extract",
            return_value=extracted,
        ):
            placeholder = _build_placeholder(setup, extracted)
            donation = create_donation_from_placeholder(
                placeholder,
                setup.scan_batch.campaign,
                setup.donation_batch,
                setup.scan_batch,
            )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.low_confidence_fields == []

    def test_high_confidence_amount_leaves_donation_pending(self) -> None:
        """All field confidences ≥ threshold ⇒ donation rides the cascade."""
        setup = _build_pipeline_setup(threshold=Decimal("0.700"))
        extracted = _stub_extracted_payload(amount_confidence=0.95)

        with patch(
            "scans.scan_processing_ocr.run_ocr_and_extract",
            return_value=extracted,
        ):
            placeholder = _build_placeholder(setup, extracted)
            donation = create_donation_from_placeholder(
                placeholder,
                setup.scan_batch.campaign,
                setup.donation_batch,
                setup.scan_batch,
            )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.low_confidence_fields == []

    def test_per_campaign_threshold_overrides_default(self) -> None:
        """Stricter thresholds still honour holds when parses fail outright."""
        setup = _build_pipeline_setup(threshold=Decimal("0.950"))
        extracted = _stub_extracted_payload(amount="", amount_confidence=0.85)

        with patch(
            "scans.scan_processing_ocr.run_ocr_and_extract",
            return_value=extracted,
        ):
            placeholder = _build_placeholder(setup, extracted)
            donation = create_donation_from_placeholder(
                placeholder,
                setup.scan_batch.campaign,
                setup.donation_batch,
                setup.scan_batch,
            )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert any(
            record["field"] == "amount" for record in donation.low_confidence_fields
        )


# ---------------------------------------------------------------------------
# E2E: full DonationBatch creation honours per-row hold records
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestE2eDonationBatchHonoursLowConfidence:
    """``create_donation_batch`` must persist low-confidence holds across rows."""

    def test_donation_batch_creates_flagged_and_clean_rows(self) -> None:
        """Placeholder with invalid donation-date confidence + malformed date."""
        setup = _build_pipeline_setup(threshold=Decimal("0.700"))

        low_conf = _stub_extracted_payload(amount="50.00", amount_confidence=0.95)
        low_conf.update(
            {
                "donation_date": "not-valid",
                "donation_date_confidence": 0.4,
            }
        )

        _build_placeholder(
            setup,
            low_conf,
        )
        _build_placeholder(
            setup,
            _stub_extracted_payload(amount="75.00", amount_confidence=0.95),
        )

        donation_batch = create_donation_batch(setup.scan_batch)

        assert donation_batch is not None
        donations = list(Donation.objects.filter(batch=donation_batch))
        assert len(donations) == 2

        flagged = next(d for d in donations if d.amount == Decimal("50.00"))
        clean = next(d for d in donations if d.amount == Decimal("75.00"))

        assert flagged.qa_status == Donation.QA_STATUS_FLAGGED
        assert any(r["field"] == "donation_date" for r in flagged.low_confidence_fields)
        assert clean.qa_status == Donation.QA_STATUS_PENDING
        assert clean.low_confidence_fields == []


# ---------------------------------------------------------------------------
# E2E: QA cascade approval respects the mandatory hold
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestE2eQaCascadeRespectsLowConfidenceHold:
    """``qa_approve_batch`` must skip donations carrying a low-confidence hold."""

    def test_cascade_keeps_held_donations_flagged_after_batch_approval(self) -> None:
        """Low-confidence donation_date stays flagged; clean donation auto-approves."""
        setup = _build_pipeline_setup(threshold=Decimal("0.700"))

        low_payload = _stub_extracted_payload(amount="40.00", amount_confidence=0.95)
        low_payload.update(
            {
                "donation_date": "!invalid!",
                "donation_date_confidence": 0.5,
            }
        )

        _build_placeholder(
            setup,
            low_payload,
        )
        _build_placeholder(
            setup,
            _stub_extracted_payload(amount="60.00", amount_confidence=0.95),
        )
        donation_batch = create_donation_batch(setup.scan_batch)
        assert donation_batch is not None

        staff = UserFactory(is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)

        response = client.post(
            reverse("custom_admin:qa_approve_batch", args=[donation_batch.id]),
            data={},
        )
        assert response.status_code == 302

        donation_batch.refresh_from_db()
        assert donation_batch.status == DonationBatch.STATUS_APPROVED

        held = Donation.objects.get(batch=donation_batch, amount=Decimal("40.00"))
        clean = Donation.objects.get(batch=donation_batch, amount=Decimal("60.00"))
        # The held donation must remain flagged — no silent auto-approve of a
        # low-confidence amount through the cascade.
        assert held.qa_status == Donation.QA_STATUS_FLAGGED
        assert any(
            r["field"] == "donation_date" for r in held.low_confidence_fields
        )
        # The clean donation auto-approves on cascade as before.
        assert clean.qa_status == Donation.QA_STATUS_APPROVED


# ---------------------------------------------------------------------------
# E2E: stubbed OCR delivers per-field confidences end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestE2eOcrStubPropagatesLowConfidence:
    """A stubbed Document AI extractor must drive the placeholder + donation state."""

    def test_stubbed_ocr_low_confidence_propagates_to_donation_hold(self) -> None:
        """``run_ocr_and_extract`` stub returns empty amount × low confidence ⇒ hold."""

        setup = _build_pipeline_setup(threshold=Decimal("0.850"))
        extracted = _stub_extracted_payload(amount="", amount_confidence=0.6)

        with patch(
            "scans.scan_processing_ocr.run_ocr_and_extract",
            return_value=extracted,
        ):
            placeholder = _build_placeholder(setup, extracted)
            donation = create_donation_from_placeholder(
                placeholder,
                setup.scan_batch.campaign,
                setup.donation_batch,
                setup.scan_batch,
            )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert "amount" in {
            record["field"] for record in donation.low_confidence_fields
        }
