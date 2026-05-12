"""Unit tests for ScanBatch layout rules."""

import pytest
from django.core.exceptions import ValidationError

from scans.models import ScanBatch
from tests.factories import ScanBatchFactory


@pytest.mark.django_db()
class TestScanBatchLayoutRules:
    """Validate batch-time scan layout rules."""

    def test_cash_allows_simplex_and_duplex_only(self) -> None:
        """Cash batches expose non-payment layouts plus legacy mixed-mail."""
        assert ScanBatch.allowed_scan_form_types_for_payment_method("cash") == (
            "simplex",
            "duplex",
            "mixed_mail",
        )

    def test_cheque_rejects_simplex_layout(self) -> None:
        """Cheque batches must use a payment-document layout."""
        batch = ScanBatchFactory.build(
            payment_method="cheque",
            scan_form_type="simplex",
        )

        with pytest.raises(ValidationError) as exc_info:
            batch.clean()

        assert "scan_form_type" in exc_info.value.message_dict

    def test_duplex_with_payment_reports_four_pages_per_donor(self) -> None:
        """Duplex payment-document batches group four pages per donor."""
        batch = ScanBatchFactory.build(scan_form_type="duplex_with_payment")

        assert batch.pages_per_donor == 4

    def test_invalid_layout_is_rejected_on_save(self) -> None:
        """Invalid payment/layout combinations cannot be persisted."""
        with pytest.raises(ValidationError) as exc_info:
            ScanBatchFactory.create(
                payment_method="cheque",
                scan_form_type="simplex",
            )

        assert "scan_form_type" in exc_info.value.message_dict

    def test_mixed_mail_can_be_saved_for_legacy_batches(self) -> None:
        """Legacy mixed-mail batches remain valid when existing rows are updated."""
        batch = ScanBatchFactory.create(
            payment_method="cash",
            scan_form_type="mixed_mail",
        )

        batch.status = ScanBatch.STATUS_PROCESSING
        batch.save()

        assert batch.scan_form_type == "mixed_mail"

    def test_normalize_batch_request_fields_trims_and_coerces_values(self) -> None:
        """Normalization should strip string fields and coerce bool-like values."""
        normalized = ScanBatch.normalize_batch_request_fields(
            r2_prefix=" ScanOutput/BRC/SPRING25/cheque/ ",
            payment_method=" cheque ",
            scan_form_type=" simplex_with_payment ",
            batch_name=" Batch-001.pdf ",
            auto_process=" false ",
        )

        assert normalized == {
            "r2_prefix": "ScanOutput/BRC/SPRING25/cheque/",
            "payment_method": "cheque",
            "scan_form_type": "simplex_with_payment",
            "batch_name": "Batch-001.pdf",
            "auto_process": False,
        }

    def test_save_allows_missing_created_by_for_programmatic_batches(self) -> None:
        """Programmatic scan-batch creation may legitimately omit a user."""
        batch = ScanBatchFactory.create(created_by=None)

        assert batch.created_by is None

    def test_final_status_is_completed_only_when_all_scans_match(self) -> None:
        """Fully matched runs should report completed."""
        assert (
            ScanBatch.final_status_for_outcomes(total=8, matched=8, failed=0)
            == ScanBatch.STATUS_COMPLETED
        )

    def test_final_status_is_partial_when_manual_review_items_remain(self) -> None:
        """Unmatched/manual-review leftovers should prevent full completion."""
        assert (
            ScanBatch.final_status_for_outcomes(total=16, matched=0, failed=0)
            == ScanBatch.STATUS_PARTIALLY_COMPLETED
        )

    def test_final_status_is_failed_when_every_scan_fails(self) -> None:
        """All-failed runs should report failed."""
        assert (
            ScanBatch.final_status_for_outcomes(total=4, matched=0, failed=4)
            == ScanBatch.STATUS_FAILED
        )
