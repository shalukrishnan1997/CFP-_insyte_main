"""Unit tests for donor-source-specific matching."""

import pytest

from campaigns.models import CampaignDataFile
from donors.models import DataFileDonor
from scans.ocr.donor_matching import load_campaign_urns, match_donor
from scans.scan_processing_donors import sync_donation_donor
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestDonorMatching:
    """Verify donor lookup obeys the configured campaign donor source."""

    # ── house_file campaigns ──────────────────────────────────────────────────

    def test_house_file_campaign_matches_house_file_directly(self) -> None:
        """A house-file campaign should match directly from the house file."""
        campaign = CampaignFactory(donor_source="house_file")
        donor = DonorFactory(urn="URN900002", client=campaign.client)

        result = match_donor(donor.urn, campaign)

        assert result["source"] == "house_file"
        assert result["donor"] == donor

    def test_house_file_campaign_returns_not_found_for_unknown_urn(self) -> None:
        """House-file campaign returns not_found if URN is absent."""
        campaign = CampaignFactory(donor_source="house_file")

        result = match_donor("URN_DOES_NOT_EXIST", campaign)

        assert result["source"] == "not_found"
        assert result["donor"] is None

    # ── data_file campaigns — primary match ──────────────────────────────────

    def test_data_file_campaign_matches_data_file_first(self) -> None:
        """A data-file campaign should match from the data file when URN exists there."""
        user = UserFactory()
        campaign = CampaignFactory(
            donor_source="data_file", campaign_temperature="warm"
        )
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        df_donor = DataFileDonor.objects.create(
            data_file=data_file,
            urn="URN900010",
            first_name="Jane",
            last_name="Smith",
        )

        result = match_donor("URN900010", campaign)

        assert result["source"] == "data_file"
        assert result["data_file_donor"] == df_donor

    # ── data_file campaigns — house file fallback ────────────────────────────

    def test_data_file_campaign_falls_back_to_house_file_when_urn_not_in_data_file(
        self,
    ) -> None:
        """When URN is absent from the data file, match should fall back to house file."""
        user = UserFactory()
        campaign = CampaignFactory(
            donor_source="data_file", campaign_temperature="warm"
        )
        donor = DonorFactory(urn="URN900020", client=campaign.client)
        # Data file exists but does NOT contain this URN
        CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        result = match_donor(donor.urn, campaign)

        assert result["source"] == "house_file"
        assert result["donor"] == donor
        assert result["data_file_donor"] is None

    def test_data_file_campaign_falls_back_to_house_file_when_no_data_file_uploaded(
        self,
    ) -> None:
        """When no data file has been uploaded yet, match falls back to house file."""
        # Campaign configured for data_file but CampaignDataFile not created
        campaign = CampaignFactory(
            donor_source="data_file", campaign_temperature="warm"
        )
        donor = DonorFactory(urn="URN900030", client=campaign.client)

        result = match_donor(donor.urn, campaign)

        assert result["source"] == "house_file"
        assert result["donor"] == donor

    def test_data_file_campaign_returns_not_found_when_urn_absent_from_both_sources(
        self,
    ) -> None:
        """If URN not in data file or house file, result is not_found."""
        user = UserFactory()
        campaign = CampaignFactory(donor_source="data_file")
        CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        result = match_donor("URN_NOWHERE", campaign)

        assert result["source"] == "not_found"
        assert result["donor"] is None
        assert result["data_file_donor"] is None

    # ── load_campaign_urns — merged URN set ──────────────────────────────────

    def test_load_campaign_urns_merges_data_file_and_house_file_urns(self) -> None:
        """For data_file campaigns, URNs from both sources must be returned."""
        user = UserFactory()
        campaign = CampaignFactory(donor_source="data_file")
        house_donor = DonorFactory(urn="HF_URN_001", client=campaign.client)
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="DF_URN_001",
            first_name="A",
            last_name="B",
        )

        urns = load_campaign_urns(campaign)

        assert "DF_URN_001" in urns
        assert house_donor.urn in urns

    def test_load_campaign_urns_no_data_file_returns_house_file_urns(self) -> None:
        """When no data file uploaded, only house file URNs are returned."""
        campaign = CampaignFactory(donor_source="data_file")
        house_donor = DonorFactory(urn="HF_URN_002", client=campaign.client)
        # No CampaignDataFile created

        urns = load_campaign_urns(campaign)

        assert house_donor.urn in urns

    def test_data_file_campaign_fallback_ignores_other_clients_house_file_donors(
        self,
    ) -> None:
        """Fallback must not match house-file donors from another client."""
        campaign = CampaignFactory(donor_source="data_file")
        other_client_campaign = CampaignFactory(donor_source="house_file")
        DonorFactory(urn="URN-OTHER-CLIENT", client=other_client_campaign.client)

        result = match_donor("URN-OTHER-CLIENT", campaign)

        assert result["source"] == "not_found"
        assert result["donor"] is None

    def test_load_campaign_urns_excludes_house_file_urns_from_other_clients(
        self,
    ) -> None:
        """House-file URN loading must stay within the campaign client."""
        campaign = CampaignFactory(donor_source="house_file")
        DonorFactory(urn="URN-SAME-CLIENT", client=campaign.client)
        DonorFactory(urn="URN-OTHER-CLIENT", client=ClientFactory())

        urns = load_campaign_urns(campaign)

        assert "URN-SAME-CLIENT" in urns
        assert "URN-OTHER-CLIENT" not in urns


@pytest.mark.django_db()
class TestSyncDonationDonor:
    """Verify sync_donation_donor does not overwrite matched donors with OCR data."""

    def _make_placeholder(self, campaign: object, extracted: dict) -> object:
        batch = ScanBatchFactory(campaign=campaign)
        return ScanPlaceholderFactory(
            batch=batch,
            extracted_data=extracted,
            ocr_status="matched",
        )

    def test_matched_donor_fields_not_overwritten_by_ocr(self) -> None:
        """For a matched donor on a normal donation campaign, OCR must not overwrite DB fields."""
        campaign = CampaignFactory(donor_source="house_file", scan_purpose="donation")
        donor = DonorFactory(
            urn="URN800001",
            address_line1="123 DB Street",
            postcode="SW1A 1AA",
            phone="07700 111111",
        )
        placeholder = self._make_placeholder(
            campaign,
            {
                "address_line1": "999 OCR Road",
                "postcode": "EC1A 1BB",
                "phone": "07700 999999",
            },
        )

        sync_donation_donor(placeholder, donor)
        donor.refresh_from_db()

        # DB values must be unchanged
        assert donor.address_line1 == "123 DB Street"
        assert donor.postcode == "SW1A 1AA"
        assert donor.phone == "07700 111111"

    def test_donor_update_campaign_does_overwrite_fields(self) -> None:
        """For a donor_update campaign, OCR values must be written to the donor record."""
        campaign = CampaignFactory(
            donor_source="house_file", scan_purpose="donor_update"
        )
        donor = DonorFactory(
            urn="URN800002",
            address_line1="Old Street",
            postcode="W1A 1AA",
        )
        placeholder = self._make_placeholder(
            campaign,
            {
                "address_line1": "New OCR Road",
                "postcode": "E1 1AA",
            },
        )

        sync_donation_donor(placeholder, donor)
        donor.refresh_from_db()

        assert donor.address_line1 == "New OCR Road"
        assert donor.postcode == "E1 1AA"

    def test_blank_fields_not_filled_for_non_donor_update_campaign(self) -> None:
        """Even blank donor fields must not be back-filled from OCR for donation campaigns."""
        campaign = CampaignFactory(donor_source="house_file", scan_purpose="donation")
        donor = DonorFactory(urn="URN800003", phone="", email="")
        placeholder = self._make_placeholder(
            campaign,
            {"phone": "07700 888888", "email": "new@example.com"},
        )

        sync_donation_donor(placeholder, donor)
        donor.refresh_from_db()

        assert donor.phone == ""
        assert donor.email == ""
