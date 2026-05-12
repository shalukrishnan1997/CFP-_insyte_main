"""Unit tests for the Jaro-Winkler donor-match false-positive guard.

These tests pin down the auto-match / candidate / no-match thresholds that
``scans.scan_processing_donors`` enforces to stop donations being credited
to the wrong donor when names overlap inside a postcode.
"""

from typing import Any
from unittest import mock

import pytest

from scans import scan_processing_donors
from scans.scan_processing_donors import (
    AUTO_MATCH_SIMILARITY,
    CANDIDATE_SIMILARITY,
    _find_existing_system_donor,
    _name_similarity,
)
from tests.factories import CampaignFactory, SystemDonorFactory


def _snapshot(first_name: str, last_name: str, postcode: str) -> dict[str, Any]:
    """Build the minimal snapshot consumed by ``_find_existing_system_donor``."""
    return {
        "external_urn": "",
        "email": "",
        "phone": "",
        "first_name": first_name,
        "last_name": last_name,
        "postcode": postcode,
    }


@pytest.mark.django_db()
class TestDonorMatchingThresholds:
    """Pin Jaro-Winkler auto-match / candidate / reject behaviour."""

    def test_exact_name_and_postcode_auto_matches(self) -> None:
        """An exact name + postcode hit must auto-link to the existing donor."""
        campaign = CampaignFactory()
        existing = SystemDonorFactory(
            client=campaign.client,
            first_name="Alice",
            last_name="Wonderland",
            postcode="SW1A 1AA",
        )

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            _snapshot("Alice", "Wonderland", "SW1A 1AA"),
        )

        assert auto == existing
        assert fuzzy == []

    def test_high_similarity_above_threshold_auto_matches(self) -> None:
        """A 0.96+ similarity name + postcode match must auto-link."""
        campaign = CampaignFactory()
        existing = SystemDonorFactory(
            client=campaign.client,
            first_name="Elizabeth",
            last_name="Thompson",
            postcode="EC1A 1BB",
        )
        # Trivial OCR variant ("Thompson" -> "Thomson") yields ~0.989 Jaro-
        # Winkler similarity over the full name (sanity-checked just below).
        candidate_first = "Elizabeth"
        candidate_last = "Thomson"
        sim = _name_similarity(
            f"{candidate_first} {candidate_last}".casefold(),
            f"{existing.first_name} {existing.last_name}".casefold(),
        )
        assert sim >= AUTO_MATCH_SIMILARITY, sim

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            _snapshot(candidate_first, candidate_last, "EC1A 1BB"),
        )

        assert auto == existing
        assert fuzzy == []

    def test_borderline_similarity_returns_candidate_no_auto_link(self) -> None:
        """Similarity in [0.85, 0.95) with postcode match must surface a fuzzy candidate."""
        campaign = CampaignFactory()
        existing = SystemDonorFactory(
            client=campaign.client,
            first_name="Catherine",
            last_name="Smith",
            postcode="W1A 1AA",
        )
        # Different first name, identical last + postcode — Jaro-Winkler over the
        # full name lands in the borderline band.
        candidate_first = "Katrina"
        candidate_last = "Smith"
        sim = _name_similarity(
            f"{candidate_first} {candidate_last}".casefold(),
            f"{existing.first_name} {existing.last_name}".casefold(),
        )
        assert CANDIDATE_SIMILARITY <= sim < AUTO_MATCH_SIMILARITY, sim

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            _snapshot(candidate_first, candidate_last, "W1A 1AA"),
        )

        assert auto is None
        # fuzzy is now a list of (donor, score) tuples; the existing donor
        # must show up with a borderline-band similarity score.
        assert any(donor == existing for donor, _ in fuzzy)
        match_score = next(score for donor, score in fuzzy if donor == existing)
        assert CANDIDATE_SIMILARITY <= match_score < AUTO_MATCH_SIMILARITY

    def test_high_similarity_with_postcode_mismatch_does_not_auto_match(self) -> None:
        """A 0.96+ similarity but mismatched postcode must NOT auto-link."""
        campaign = CampaignFactory()
        SystemDonorFactory(
            client=campaign.client,
            first_name="Elizabeth",
            last_name="Thompson",
            postcode="EC1A 1BB",
        )

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            _snapshot("Elizabeth", "Thomson", "SW1A 1AA"),
        )

        assert auto is None
        # Postcode mismatch is a hard reject — we don't even surface candidates.
        assert fuzzy == []

    def test_low_similarity_returns_no_candidate(self) -> None:
        """A name similarity below 0.85 must produce no match suggestion."""
        campaign = CampaignFactory()
        SystemDonorFactory(
            client=campaign.client,
            first_name="Alice",
            last_name="Wonderland",
            postcode="SW1A 1AA",
        )
        # Wildly different name, same postcode.
        sim = _name_similarity(
            "bob jones",
            "alice wonderland",
        )
        assert sim < CANDIDATE_SIMILARITY, sim

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            _snapshot("Bob", "Jones", "SW1A 1AA"),
        )

        assert auto is None
        assert fuzzy == []

    def test_postcode_prefilter_skips_donors_at_other_postcodes(self) -> None:
        """The matcher must scan only donors at the target postcode.

        Without the DB-level postcode prefilter, every placeholder forces a
        full ``SystemDonor`` table scan for the client (an O(N) Python-side
        loop). For clients with 100k+ donors that is catastrophic. This test
        creates donors across many postcodes and asserts the Jaro-Winkler
        similarity helper is only invoked for rows at the target postcode.
        """
        campaign = CampaignFactory()
        target_postcode = "SW1A 1AA"
        target_count = 3
        other_count = 50  # kept modest so the test stays fast in CI

        # Donors at the target postcode — these are the only rows the
        # matcher should score.
        for i in range(target_count):
            SystemDonorFactory(
                client=campaign.client,
                first_name=f"Target{i}",
                last_name="Donor",
                postcode=target_postcode,
            )
        # Donors at other postcodes that must be filtered out at the DB
        # layer before the Python-side similarity scan runs.
        for i in range(other_count):
            SystemDonorFactory(
                client=campaign.client,
                first_name=f"Other{i}",
                last_name="Donor",
                # Vary the postcode per row so none collides with the target.
                postcode=f"N{i % 9 + 1} {i % 9 + 1}AA",
            )

        with mock.patch.object(
            scan_processing_donors,
            "_name_similarity",
            wraps=scan_processing_donors._name_similarity,
        ) as spy:
            auto, fuzzy = _find_existing_system_donor(
                campaign,
                _snapshot("Brand", "New", target_postcode),
            )

        # Brand-new name -> no auto-match, no candidates.
        assert auto is None
        assert fuzzy == []
        # Crucially: we only scored donors at the target postcode, never
        # the 50 donors at other postcodes.
        assert spy.call_count == target_count, (
            f"expected {target_count} similarity scores (one per donor at the "
            f"target postcode), got {spy.call_count} — DB-level postcode "
            "prefilter regressed."
        )

    def test_postcode_prefilter_matches_compact_stored_form(self) -> None:
        """Stored compact postcodes (no inward space) must still be found.

        The DB prefilter queries both the canonical spaced form and the
        compact form so donors stored either way are still candidates for
        the Python-side similarity scan.
        """
        campaign = CampaignFactory()
        existing = SystemDonorFactory(
            client=campaign.client,
            first_name="Alice",
            last_name="Wonderland",
            # Stored in compact form (no inward-code space).
            postcode="SW1A1AA",
        )

        auto, fuzzy = _find_existing_system_donor(
            campaign,
            # Input arrives spaced — should still match the compact row.
            _snapshot("Alice", "Wonderland", "SW1A 1AA"),
        )

        assert auto == existing
        assert fuzzy == []
