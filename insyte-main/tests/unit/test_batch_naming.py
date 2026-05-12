"""Unit tests for canonical physical batch naming."""

from typing import Any
from unittest.mock import patch

import pytest

from scans.batch_naming import derive_batch_identity, normalize_batch_name
from scans.scan_processing import ScanProcessingService
from scans.scan_processing_donations import create_donation_batch
from tests.factories import (
    CampaignFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestBatchNamingHelpers:
    """Validate canonical batch-name derivation rules."""

    def test_normalize_batch_name_strips_pdf_extension(self) -> None:
        """Physical batch names should be stored without the PDF suffix."""
        assert normalize_batch_name("  Batch-001.pdf  ") == "Batch-001"

    def test_derive_batch_identity_uses_virtual_pdf_source_key(self) -> None:
        """Virtual page keys should resolve back to the original PDF filename."""
        identity = derive_batch_identity(
            [
                "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf::pdf_page::0001",
                "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf::pdf_page::0002",
            ]
        )

        assert identity.batch_name == "Batch-001"
        assert identity.source_filename == "Batch-001.pdf"

    def test_derive_batch_identity_rejects_multiple_source_files(self) -> None:
        """A physical batch must map to exactly one uploaded PDF."""
        with pytest.raises(ValueError, match="exactly one PDF file"):
            derive_batch_identity(
                [
                    "ScanOutput/BRC/SPRING25/cheque/Batch-001.pdf",
                    "ScanOutput/BRC/SPRING25/cheque/Batch-002.pdf",
                ]
            )


@pytest.mark.django_db()
class TestScanProcessingBatchNaming:
    """Validate scan-batch creation with canonical physical identifiers."""

    @patch("scans.scan_processing_r2.expand_pdf_keys_for_campaign")
    def test_create_scan_batch_uses_pdf_filename_for_batch_name(
        self,
        mock_expand_pdf_keys: Any,
    ) -> None:
        """The uploaded PDF filename should drive scan batch naming."""
        campaign = CampaignFactory()
        user = UserFactory()
        source_key = "ScanOutput/BRC/SPRING25/cheque/Batch-101.pdf"
        mock_expand_pdf_keys.return_value = [
            f"{source_key}::pdf_page::0001",
            f"{source_key}::pdf_page::0002",
            f"{source_key}::pdf_page::0003",
            f"{source_key}::pdf_page::0004",
        ]

        scan_batch = ScanProcessingService.create_scan_batch_from_r2(
            campaign_id=str(campaign.id),
            r2_keys=[source_key],
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            user=user,
        )

        assert scan_batch.batch_name == "Batch-101"
        assert scan_batch.source_filename == "Batch-101.pdf"
        assert scan_batch.total_scans == 2

        donor_pdf_keys = list(
            scan_batch.placeholders.order_by("created_at").values_list(
                "donor_pdf_key", flat=True
            )
        )
        assert donor_pdf_keys == ["", ""]

    @patch("scans.scan_processing_r2.expand_pdf_keys_for_campaign")
    def test_create_scan_batch_rejects_duplicate_batch_name(
        self,
        mock_expand_pdf_keys: Any,
    ) -> None:
        """Duplicate batch identifiers in the same campaign should fail fast."""
        campaign = CampaignFactory()
        source_key = "ScanOutput/BRC/SPRING25/cheque/Batch-202.pdf"
        ScanBatchFactory(
            campaign=campaign,
            batch_name="Batch-202",
            source_filename="Batch-202.pdf",
        )
        mock_expand_pdf_keys.return_value = [
            f"{source_key}::pdf_page::0001",
            f"{source_key}::pdf_page::0002",
        ]

        with pytest.raises(ValueError, match="already exists"):
            ScanProcessingService.create_scan_batch_from_r2(
                campaign_id=str(campaign.id),
                r2_keys=[source_key],
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
            )

    @patch("scans.scan_processing_r2.expand_pdf_keys_for_campaign")
    def test_create_scan_batch_rejects_oversized_physical_batch(
        self,
        mock_expand_pdf_keys: Any,
    ) -> None:
        """A scanned physical batch cannot exceed the configured donor-form limit."""
        campaign = CampaignFactory()
        source_key = "ScanOutput/BRC/SPRING25/cheque/Batch-303.pdf"
        mock_expand_pdf_keys.return_value = [
            f"{source_key}::pdf_page::{page_number:04d}" for page_number in range(1, 63)
        ]

        with pytest.raises(ValueError, match="maximum allowed size of 30 donor forms"):
            ScanProcessingService.create_scan_batch_from_r2(
                campaign_id=str(campaign.id),
                r2_keys=[source_key],
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
            )

    @patch("scans.models.ScanBatch.full_clean")
    @patch("scans.scan_processing._validate_batch_uniqueness")
    @patch("scans.scan_processing_r2.expand_pdf_keys_for_campaign")
    def test_concurrent_duplicate_batch_raises_value_error(
        self,
        mock_expand_pdf_keys: Any,
        _mock_validate_uniqueness: Any,
        _mock_full_clean: Any,
    ) -> None:
        """A racing duplicate insert surfaces as ValueError, not IntegrityError.

        Both pre-insert checks (``_validate_batch_uniqueness`` and Django's
        ``full_clean`` invoked from ``ScanBatch.save``) are stubbed to model
        the race window, so the unique constraint is the only thing that
        catches the duplicate.
        """
        campaign = CampaignFactory()
        source_key = "ScanOutput/BRC/SPRING25/cheque/Batch-505.pdf"
        ScanBatchFactory(
            campaign=campaign,
            batch_name="Batch-505",
            source_filename="Batch-505.pdf",
        )
        mock_expand_pdf_keys.return_value = [
            f"{source_key}::pdf_page::0001",
            f"{source_key}::pdf_page::0002",
        ]

        with pytest.raises(ValueError, match="already exists"):
            ScanProcessingService.create_scan_batch_from_r2(
                campaign_id=str(campaign.id),
                r2_keys=[source_key],
                payment_method="cheque",
                scan_form_type="simplex_with_payment",
            )


@pytest.mark.django_db()
class TestDonationBatchNaming:
    """Validate downstream donation-batch naming from scan batches."""

    @patch("scans.scan_processing_donations.create_donation_from_placeholder")
    def test_create_donation_batch_keeps_scan_batch_name(
        self,
        mock_create_donation: Any,
    ) -> None:
        """Donation batches should retain the same physical batch identifier."""
        scan_batch = ScanBatchFactory(
            batch_name="Batch-404",
            source_filename="Batch-404.pdf",
        )
        ScanPlaceholderFactory(
            batch=scan_batch,
            ocr_status="matched",
            is_captured=False,
        )

        donation_batch = create_donation_batch(scan_batch)

        assert donation_batch is not None
        assert donation_batch.batch_name == "Batch-404"
        mock_create_donation.assert_called_once()
