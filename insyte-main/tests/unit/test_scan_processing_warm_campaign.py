"""Unit tests for warm-campaign scan processing exceptions."""

import pytest

from campaigns.models import CampaignDataFile
from donors.models import DataFileDonor, Donor, SystemDonor
from scans.models import ScanPlaceholder
from scans.scan_processing import ScanProcessingService
from scans.scan_processing_donors import _apply_warm_qr_failure
from tests.factories import (
    CampaignFactory,
    DonorFactory,
    ScanPlaceholderFactory,
    SystemDonorFactory,
)


@pytest.mark.django_db()
class TestWarmCampaignScanProcessing:
    """Ensure warm campaigns do not create donors automatically."""

    def test_warm_campaign_without_qr_goes_to_manual_review(self) -> None:
        """Unreadable QR on a warm campaign should not create a donor."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN100001",
            ocr_data={},
        )

        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "manual_review"
        assert placeholder.matched_donor is None
        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_NEEDS_RESCAN
        assert placeholder.ocr_data["donor_match_status"] == "manual_review"
        assert (
            placeholder.ocr_data["exception_reason"]
            == "qr_unreadable_for_warm_campaign"
        )

    def test_apply_warm_qr_failure_sets_needs_rescan_status(self) -> None:
        """Warm-QR failure must use the dedicated NEEDS_RESCAN status, not COMPLETED."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            urn="URN999999",
            donor_name="Stale Donor",
            ocr_data={},
            ocr_status=ScanPlaceholder.OCR_STATUS_PROCESSING,
        )

        _apply_warm_qr_failure(placeholder, campaign, "qr_unreadable_for_warm_campaign")

        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_NEEDS_RESCAN
        assert placeholder.ocr_status != ScanPlaceholder.OCR_STATUS_COMPLETED
        assert placeholder.urn == ""
        assert placeholder.donor_name == ""
        assert placeholder.matched_donor is None
        assert placeholder.matched_data_file_donor is None
        assert (
            "Warm campaign requires a readable QR code" in placeholder.processing_error
        )

    def test_apply_warm_qr_failure_source_miss_uses_needs_rescan(self) -> None:
        """Warm source-miss path also routes through NEEDS_RESCAN."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="data_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            urn="URN_MISSING",
            ocr_data={},
            ocr_status=ScanPlaceholder.OCR_STATUS_PROCESSING,
        )

        _apply_warm_qr_failure(
            placeholder,
            campaign,
            "warm_source_miss_rescan_under_cold_campaign",
        )

        assert placeholder.ocr_status == ScanPlaceholder.OCR_STATUS_NEEDS_RESCAN

    def test_warm_campaign_source_miss_does_not_create_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Warm source misses should be flagged for rescan, not donor creation."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="data_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=True,
            urn="URN100002",
            ocr_data={},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": None,
                "data_file_donor": None,
                "source": "not_found",
                "donor_name": "",
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        donor_count_before = Donor.objects.count()
        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "manual_review"
        assert Donor.objects.count() == donor_count_before
        assert placeholder.matched_donor is None
        assert placeholder.ocr_data["identifier_source"] == "qr"
        assert placeholder.ocr_data["donor_match_status"] == "manual_review"
        assert (
            placeholder.ocr_data["exception_reason"]
            == "warm_source_miss_rescan_under_cold_campaign"
        )

    def test_warm_campaign_qr_match_prefers_data_file_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Warm QR matches should attach campaign data-file donors first."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="data_file",
        )
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=None)
        data_file_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="URN100020",
            first_name="Dora",
            last_name="Data",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=True,
            urn="URN100020",
            ocr_data={},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": None,
                "data_file_donor": data_file_donor,
                "source": "data_file",
                "donor_name": "Dora Data",
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "data_file"
        assert placeholder.matched_data_file_donor == data_file_donor
        assert placeholder.matched_donor is None
        assert placeholder.ocr_status == "matched"
        assert placeholder.ocr_data["donor_match_status"] == "matched"

    def test_warm_campaign_qr_match_supports_house_file_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Warm QR matches should still support house-file donors when returned."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="house_file",
        )
        donor = DonorFactory(client=campaign.client, urn="URN100021")
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=True,
            urn="URN100021",
            ocr_data={},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": donor,
                "data_file_donor": None,
                "source": "house_file",
                "donor_name": donor.full_name,
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "house_file"
        assert placeholder.matched_donor == donor
        assert placeholder.matched_data_file_donor is None
        assert placeholder.ocr_status == "matched"
        assert placeholder.ocr_data["donor_match_status"] == "matched"

    def test_cold_campaign_ocr_match_uses_existing_house_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cold records should link to existing house-file donors when matched."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        donor = DonorFactory(client=campaign.client, urn="URN100022")
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN100022",
            ocr_data={},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": donor,
                "data_file_donor": None,
                "source": "house_file",
                "donor_name": donor.full_name,
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        donor_count_before = Donor.objects.count()
        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "house_file"
        assert placeholder.matched_donor == donor
        assert placeholder.ocr_data["donor_match_status"] == "matched"
        assert Donor.objects.count() == donor_count_before

    def test_cold_campaign_source_miss_creates_new_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cold campaigns keep the current auto-create donor workflow."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN100003",
            ocr_data={},
            extracted_data={"donor_name": "Jane Example", "postcode": "SW1A 1AA"},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": None,
                "data_file_donor": None,
                "source": "not_found",
                "donor_name": "",
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        donor_count_before = SystemDonor.objects.count()
        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "house_file"
        assert SystemDonor.objects.count() == donor_count_before + 1
        assert placeholder.matched_system_donor is not None
        assert placeholder.matched_system_donor.client == campaign.client
        assert placeholder.matched_donor is None
        assert placeholder.matched_system_donor.pending_review is True
        assert placeholder.ocr_data["donor_match_status"] == "new_donor_created"

    def test_cold_campaign_source_miss_reuses_existing_placeholder_donor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repeated cold-campaign scans should reuse the same unresolved donor."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        existing_donor = DonorFactory(
            client=campaign.client,
            urn=None,
            first_name="Jane",
            last_name="Example",
            postcode="SW1A 1AA",
            verification_status=Donor.VERIFICATION_PENDING_EXPORT,
        )
        existing_system_donor = SystemDonorFactory(
            client=campaign.client,
            external_urn="",
            first_name=existing_donor.first_name,
            last_name=existing_donor.last_name,
            postcode=existing_donor.postcode,
            created_by=existing_donor.created_by,
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN100004",
            ocr_data={},
            extracted_data={"donor_name": "Jane Example", "postcode": "SW1A1AA"},
        )

        def fake_match_donor(_urn: str, _campaign: object) -> dict[str, object]:
            return {
                "donor": None,
                "data_file_donor": None,
                "source": "not_found",
                "donor_name": "",
            }

        monkeypatch.setattr(
            "scans.ocr.OCRExtractor.match_donor",
            fake_match_donor,
        )

        donor_count_before = SystemDonor.objects.count()
        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "house_file"
        assert SystemDonor.objects.count() == donor_count_before
        assert placeholder.matched_donor is None
        assert placeholder.matched_system_donor == existing_system_donor
        assert placeholder.ocr_data["donor_match_status"] == "new_donor_created"

    def test_warm_campaign_malformed_qr_flags_for_rescan(self) -> None:
        """A detected-but-corrupt QR on a warm campaign must surface a rescan reason."""
        campaign = CampaignFactory(
            campaign_temperature="warm",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="",
            ocr_data={"qr_kind": "malformed", "qr_error": "raw='garbage'"},
        )

        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "manual_review"
        assert placeholder.matched_donor is None
        assert (
            placeholder.ocr_data["exception_reason"] == "qr_malformed_for_warm_campaign"
        )
        assert "corrupt" in placeholder.processing_error
        assert "raw='garbage'" in placeholder.processing_error

    def test_cold_campaign_malformed_qr_flags_for_rescan(self) -> None:
        """A detected-but-corrupt QR on a cold campaign must also flag for rescan."""
        campaign = CampaignFactory(
            campaign_temperature="cold",
            donor_source="house_file",
        )
        placeholder = ScanPlaceholderFactory(
            batch__campaign=campaign,
            qr_decoded=False,
            urn="URN-COLD-MALFORMED",
            ocr_data={"qr_kind": "malformed", "qr_error": "raw='oops'"},
        )

        donor_count_before = Donor.objects.count()
        result = ScanProcessingService._apply_donor_match(  # pyright: ignore[reportPrivateUsage]
            placeholder,
            campaign,
        )

        assert result["source"] == "manual_review"
        assert Donor.objects.count() == donor_count_before
        assert placeholder.matched_donor is None
        assert placeholder.ocr_data["exception_reason"] == "qr_malformed_payload"
        assert "corrupt" in placeholder.processing_error
