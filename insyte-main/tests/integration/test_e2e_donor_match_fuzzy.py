"""End-to-end integration tests for fuzzy donor-match candidate persistence.

These tests close the "unit 24" hardening gap: when the donor matcher
finds two-or-more high-similarity donors above the candidate threshold but
none clears the auto-match bar, the QA workflow needs the candidate list
persisted on the placeholder so a reviewer can pick the right donor.

Drives :class:`scans.scan_processing.ScanProcessingService.process_single_scan`
end-to-end with a stubbed OCR layer so the matching path runs against real
``SystemDonor`` rows in the test DB. The Document AI / R2 calls are stubbed
out to keep the test hermetic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from scans.models import ScanPlaceholder
from scans.scan_processing import ScanProcessingService
from tests.factories import (
    CampaignFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    SystemDonorFactory,
)


def _ocr_stub(
    payload: dict[str, Any],
) -> Callable[[Any, Any, list[str] | None], dict[str, Any]]:
    """Build a ``run_ocr_and_extract`` stub that writes ``payload`` to the placeholder."""

    def _stub(
        placeholder: Any,
        _campaign: Any,
        _known_urns: list[str] | None,
    ) -> dict[str, Any]:
        placeholder.extracted_data = payload
        placeholder.ocr_data = {"qr_kind": ""}
        placeholder.ocr_confidence = 0.95
        return payload

    return _stub


@pytest.mark.django_db()
class TestFuzzyDonorMatchCandidatesPersistedForQA:
    """Persist borderline donor candidates on the scan placeholder.

    When the matcher scores 2+ donors inside the candidate band
    (Jaro-Winkler ``[0.85, 0.95)``) for the same postcode, none auto-link.
    The new ``ScanPlaceholder.donor_match_candidates`` JSON field carries
    the disambiguation set forward to the QA UI.
    """

    def test_two_high_similarity_donors_persisted_as_match_candidates(
        self,
    ) -> None:
        """Two borderline donors land in the placeholder's candidate list."""
        campaign = CampaignFactory(campaign_temperature="cold")
        # Two SystemDonors at the same postcode whose names land inside the
        # borderline Jaro-Winkler band against "Jonh Smyth":
        #   * "John Smith"     -> ~0.917 (candidate)
        #   * "Jonathan Smyth" -> ~0.863 (candidate)
        # Neither breaches the 0.95 auto-match bar, so both must be surfaced
        # for QA disambiguation.
        donor_a = SystemDonorFactory(
            client=campaign.client,
            first_name="John",
            last_name="Smith",
            postcode="SW1A 1AA",
            external_urn="HOUSE-JS-001",
        )
        donor_b = SystemDonorFactory(
            client=campaign.client,
            first_name="Jonathan",
            last_name="Smyth",
            postcode="SW1A 1AA",
            external_urn="HOUSE-JS-002",
        )

        scan_batch = ScanBatchFactory(
            campaign=campaign,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
            urn="",
            donor_name="",
            extracted_data={},
            is_captured=False,
        )

        ocr_payload: dict[str, Any] = {
            "donor_name": "Jonh Smyth",
            "postcode": "SW1A 1AA",
            "amount": "25.00",
            "gift_aid": False,
            "urn": "",
            "urn_confidence": 0.0,
        }

        # Stub the OCR layer so the matching path runs without a real
        # Document AI / R2 round-trip.
        with patch(
            "scans.scan_processing.run_ocr_and_extract",
            side_effect=_ocr_stub(ocr_payload),
        ):
            ScanProcessingService.process_single_scan(str(placeholder.id))

        placeholder.refresh_from_db()

        # Cold-campaign URN miss creates a new SystemDonor and leaves the
        # house-file / data-file FKs cleared — that's the expected "needs QA"
        # shape we surface candidates against.
        assert placeholder.matched_donor is None
        assert placeholder.matched_data_file_donor is None

        candidates = list(placeholder.donor_match_candidates)
        assert len(candidates) >= 2, candidates

        candidate_ids = {entry["system_donor_id"] for entry in candidates}
        assert {str(donor_a.pk), str(donor_b.pk)}.issubset(candidate_ids)

        urns = {entry["urn"] for entry in candidates}
        assert {donor_a.external_urn, donor_b.external_urn}.issubset(urns)

        # Each candidate must carry a numeric score in the borderline band
        # so the QA UI can render a confidence ranking without a follow-up
        # DB lookup.
        for entry in candidates:
            assert isinstance(entry["score"], (int, float))
            assert 0.85 <= float(entry["score"]) < 0.95

        # Candidates are ordered by descending score so the strongest match
        # appears first in the QA list.
        scores = [float(entry["score"]) for entry in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_exact_match_clears_candidate_list(self) -> None:
        """An auto-match donor must not leave fuzzy candidates lingering.

        Companion to the borderline test: when one donor crosses the
        auto-match bar, ``_name_postcode_match`` deliberately drops the
        fuzzy list so the QA UI doesn't see misleading "alternates".
        """
        campaign = CampaignFactory(campaign_temperature="cold")
        SystemDonorFactory(
            client=campaign.client,
            first_name="Elizabeth",
            last_name="Thompson",
            postcode="EC1A 1BB",
            external_urn="HOUSE-ET-001",
        )
        # A second borderline donor that would be a candidate if the first
        # didn't auto-match.
        SystemDonorFactory(
            client=campaign.client,
            first_name="Beth",
            last_name="Thomson",
            postcode="EC1A 1BB",
            external_urn="HOUSE-ET-002",
        )

        scan_batch = ScanBatchFactory(
            campaign=campaign,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
            urn="",
            donor_name="",
            extracted_data={},
            is_captured=False,
        )

        ocr_payload: dict[str, Any] = {
            "donor_name": "Elizabeth Thomson",  # ~0.989 vs "Elizabeth Thompson"
            "postcode": "EC1A 1BB",
            "amount": "10.00",
            "gift_aid": False,
            "urn": "",
            "urn_confidence": 0.0,
        }

        with patch(
            "scans.scan_processing.run_ocr_and_extract",
            side_effect=_ocr_stub(ocr_payload),
        ):
            ScanProcessingService.process_single_scan(str(placeholder.id))

        placeholder.refresh_from_db()
        assert placeholder.donor_match_candidates == []

    def test_structured_candidates_propagate_to_donation(self) -> None:
        """``Donation.donor_match_candidates`` must mirror the placeholder payload.

        The whole point of the structured field on the placeholder is that
        QA can disambiguate without a follow-up DB lookup. That contract
        breaks if the donation creation step throws away the structured
        dicts and re-derives a bare PK list, so this test pins the
        propagation: the donation's stored payload must equal the
        placeholder's, dict-for-dict.
        """
        from donations.models import Donation
        from scans.scan_processing_donations import create_donation_from_placeholder
        from tests.factories import DonationBatchFactory

        campaign = CampaignFactory(campaign_temperature="cold")
        donor_a = SystemDonorFactory(
            client=campaign.client,
            first_name="John",
            last_name="Smith",
            postcode="SW1A 1AA",
            external_urn="HOUSE-PROP-001",
        )
        donor_b = SystemDonorFactory(
            client=campaign.client,
            first_name="Jonathan",
            last_name="Smyth",
            postcode="SW1A 1AA",
            external_urn="HOUSE-PROP-002",
        )

        scan_batch = ScanBatchFactory(
            campaign=campaign,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_PENDING,
            urn="",
            donor_name="",
            extracted_data={},
            is_captured=False,
        )

        ocr_payload: dict[str, Any] = {
            "donor_name": "Jonh Smyth",
            "postcode": "SW1A 1AA",
            "amount": "25.00",
            "gift_aid": False,
            "urn": "",
            "urn_confidence": 0.0,
        }

        with patch(
            "scans.scan_processing.run_ocr_and_extract",
            side_effect=_ocr_stub(ocr_payload),
        ):
            ScanProcessingService.process_single_scan(str(placeholder.id))

        placeholder.refresh_from_db()
        placeholder_candidates = list(placeholder.donor_match_candidates)
        assert placeholder_candidates, "fuzzy candidates must be on placeholder"
        candidate_donor_ids = {
            entry["system_donor_id"] for entry in placeholder_candidates
        }
        assert {str(donor_a.pk), str(donor_b.pk)}.issubset(candidate_donor_ids)

        # Drive the donation-creation step that the QA-approval pipeline
        # eventually runs on the placeholder. The donation's
        # donor_match_candidates payload must be the structured
        # placeholder list, not a bare PK list re-derived from ocr_data.
        donation_batch = DonationBatchFactory(campaign=campaign)
        donation = create_donation_from_placeholder(
            placeholder, campaign, donation_batch, scan_batch
        )

        assert donation is not None
        # Structured shape preserved end-to-end.
        assert donation.donor_match_candidates == placeholder_candidates
        # Each entry must be the structured dict shape, not a bare string.
        for entry in donation.donor_match_candidates:
            assert isinstance(entry, dict)
            assert {"urn", "score", "name", "system_donor_id"}.issubset(entry.keys())
        # And the QA hold is engaged so the borderline donation cannot be
        # auto-approved without a reviewer disambiguating the donor.
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    def test_legacy_bare_pk_payload_still_propagates_to_donation(self) -> None:
        """Pre-migration placeholders writing only the legacy PK list must still flow.

        Until every placeholder has been re-processed under the structured
        field, ``create_donation_from_placeholder`` must honour the legacy
        ``ocr_data["donor_match_candidates"]`` PK list as a fallback when
        the structured field is empty. This guards the dual-write window
        documented on
        :func:`scans.scan_processing_donors._record_fuzzy_candidates`.
        """
        from donations.models import Donation
        from scans.scan_processing_donations import create_donation_from_placeholder
        from tests.factories import DonationBatchFactory

        campaign = CampaignFactory(campaign_temperature="cold")
        donor = SystemDonorFactory(
            client=campaign.client,
            first_name="Alice",
            last_name="Wonderland",
            postcode="SW1A 1AA",
        )
        scan_batch = ScanBatchFactory(
            campaign=campaign,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
        )
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status=ScanPlaceholder.OCR_STATUS_UNMATCHED,
            urn="",
            donor_name="",
            extracted_data={
                "donor_name": "Alice Wonderland",
                "amount": "25.00",
                "donation_date": "01/01/2026",
                "payment_method": "cheque",
            },
            ocr_data={"donor_match_candidates": [str(donor.pk)]},
            ocr_confidence=0.95,
            # Structured field deliberately empty to mimic a placeholder
            # written before unit 24 shipped.
            donor_match_candidates=[],
            is_captured=False,
        )

        donation_batch = DonationBatchFactory(campaign=campaign)
        donation = create_donation_from_placeholder(
            placeholder, campaign, donation_batch, scan_batch
        )

        assert donation is not None
        assert donation.donor_match_candidates == [str(donor.pk)]
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
