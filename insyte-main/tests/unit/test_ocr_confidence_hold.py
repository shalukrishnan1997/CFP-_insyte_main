"""Unit tests for OCR confidence threshold + mandatory QA hold (Issue #23).

Two surfaces are exercised:

1. ``scans.scan_processing_donations.create_donation_from_placeholder`` — the
   builder must populate ``Donation.low_confidence_fields`` when the OCR
   amount/date confidence falls below the campaign-configurable threshold.
2. ``custom_admin.views.qa_review.qa_approve_batch`` — the cascade
   auto-approve on batch approval must skip donations with non-empty
   ``low_confidence_fields`` so a silent-accept-of-wrong-amount cannot reach
   approved state via the convenience cascade.
"""

from decimal import Decimal

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from custom_admin.views import qa_review
from donations.models import Donation, DonationBatch
from scans.scan_processing_donations import create_donation_from_placeholder
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    SystemDonorFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_message(_request: object, _message: object) -> None:
    """Suppress Django flash messages during unit tests."""


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, _noop_message)
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(qa_review.cache, "delete", lambda _k: None)


def _staff_post_request() -> HttpRequest:
    """Return a POST RequestFactory instance (without user attached)."""
    return RequestFactory().post("/admin/qa/batch/approve/")


# ---------------------------------------------------------------------------
# Detection — create_donation_from_placeholder populates low_confidence_fields
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestLowConfidenceDetection:
    """OCR confidence threshold flags and records offending fields."""

    def test_low_amount_confidence_flags_and_records_field(self) -> None:
        """Blank amount with low confidence → mandatory hold + qa flag."""
        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        # Default campaign threshold is 0.700 — 0.55 must trigger only when
        # the amount is actually missing / structurally invalid even though
        # Document AI emits a sub-threshold score.
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "",
                "amount_confidence": 0.55,
                "cheque_date": "01/06/2026",
                "donation_date_confidence": 0.95,
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert donation.low_confidence_fields
        records = donation.low_confidence_fields
        assert any(r["field"] == "amount" for r in records)
        amount_record = next(r for r in records if r["field"] == "amount")
        assert pytest.approx(float(amount_record["confidence"]), abs=1e-3) == 0.55

    def test_structurally_sound_amount_below_threshold_skips_hold(self) -> None:
        """Form Parser jitter (≈0.55) should not block once amount parses cleanly."""
        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "1500.00",
                "amount_confidence": 0.55,
                "donation_date_confidence": 0.95,
                "donation_date": "03/06/2026",
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.low_confidence_fields == []

    def test_high_amount_confidence_leaves_low_confidence_fields_empty(self) -> None:
        """Mocked OCR amount confidence 0.9 — donation auto-approves normally."""
        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "25.00",
                "cheque_date": "01/05/2026",
                "amount_confidence": 0.9,
                "donation_date_confidence": 0.95,
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.low_confidence_fields == []

    def test_low_donor_urn_confidence_does_not_trigger_hold(self) -> None:
        """``donor_urn`` flags qa_notes but is excluded from the cascade hold.

        Issue #23 only mandates the hold for amount + donation_date (the fields
        whose silent acceptance would push wrong financials downstream). Donor
        identity is resolved by the matcher; a low URN confidence reading
        should not by itself force a per-donation review on every batch.
        """
        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "25.00",
                "cheque_date": "01/05/2026",
                "amount_confidence": 0.95,
                "donation_date_confidence": 0.95,
                "urn_confidence": 0.10,
                "payment_method_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        # Hold list stays empty so the donation rides the cascade auto-approve.
        assert donation.low_confidence_fields == []

    def test_per_campaign_threshold_overrides_default(self) -> None:
        """A stricter campaign threshold must catch reads that the default permits."""
        matched_donor = DonorFactory()
        system_donor = SystemDonorFactory(client=matched_donor.client)
        scan_batch = ScanBatchFactory(payment_method="cheque")
        # Bump the campaign threshold above 0.85 so a 0.8 read is now "low".
        scan_batch.campaign.ocr_confidence_threshold = Decimal("0.900")
        scan_batch.campaign.save(update_fields=["ocr_confidence_threshold"])
        donation_batch = DonationBatchFactory(campaign=scan_batch.campaign)
        placeholder = ScanPlaceholderFactory(
            batch=scan_batch,
            matched_donor=matched_donor,
            matched_system_donor=system_donor,
            ocr_confidence=0.99,
            ocr_data={"donor_match_status": "matched"},
            extracted_data={
                "amount": "",
                "amount_confidence": 0.8,
                "donation_date_confidence": 0.95,
                "urn_confidence": 0.95,
                "payment_method_confidence": 0.95,
            },
        )

        donation = create_donation_from_placeholder(
            placeholder,
            scan_batch.campaign,
            donation_batch,
            scan_batch,
        )

        assert donation is not None
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert any(r["field"] == "amount" for r in donation.low_confidence_fields)


# ---------------------------------------------------------------------------
# QA hold — qa_approve_batch cascade skips low_confidence donations
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestQaCascadeRespectsLowConfidenceHold:
    """Cascade auto-approve must NOT touch donations flagged for mandatory review."""

    def test_cascade_skips_low_confidence_held_donation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Low-confidence amount: donation stays flagged after batch approval."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )
        held_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_FLAGGED,
            low_confidence_fields=[{"field": "amount", "confidence": 0.5}],
        )
        clean_donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
            low_confidence_fields=[],
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        held_donation.refresh_from_db()
        clean_donation.refresh_from_db()
        # Clean donation rides the cascade.
        assert clean_donation.qa_status == Donation.QA_STATUS_APPROVED
        # Held donation stays flagged — explicit reviewer action required.
        assert held_donation.qa_status == Donation.QA_STATUS_FLAGGED

    def test_cascade_auto_approves_when_no_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """High-confidence (empty hold list) donations auto-approve normally."""
        _patch_messages(monkeypatch)
        staff = UserFactory(is_staff=True, is_superuser=True)
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_PENDING_QA,
            default_payment_method="caf",
        )
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="caf",
            qa_status=Donation.QA_STATUS_PENDING,
            low_confidence_fields=[],
        )

        request = _staff_post_request()
        request.user = staff

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
