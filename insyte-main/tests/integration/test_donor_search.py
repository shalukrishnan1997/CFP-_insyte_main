from typing import Any

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse

from campaigns.models import CampaignDataFile
from donors.models import DataFileDonor
from tests.factories import CampaignFactory, ClientFactory, DonorFactory, UserFactory


@pytest.mark.django_db
class TestDonorSearch:
    """Integration tests for donor search source selection and fallback."""

    def test_house_file_search_is_scoped_to_campaign_client(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(donor_source="house_file")
        DonorFactory(
            client=campaign.client,
            urn="URN-CLIENT-A",
            first_name="John",
            last_name="Scoped",
        )
        DonorFactory(
            client=ClientFactory(),
            urn="URN-CLIENT-B",
            first_name="John",
            last_name="Scoped",
        )

        response = authenticated_client.get(
            reverse("custom_admin:donor_search"),
            {"q": "Scoped", "campaign_id": str(campaign.id)},
        )

        payload = response.json()

        assert response.status_code == 200
        assert len(payload["donors"]) == 1
        assert payload["donors"][0]["urn"] == "URN-CLIENT-A"

    def test_data_file_search_falls_back_to_same_client_house_file(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        user = UserFactory()
        campaign = CampaignFactory(donor_source="data_file")
        CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        DonorFactory(
            client=campaign.client,
            urn="URN-HOUSE-FALLBACK",
            first_name="Jane",
            last_name="Fallback",
            country="Ireland",
            gift_aid_declaration=True,
        )
        DonorFactory(
            client=ClientFactory(),
            urn="URN-OTHER-CLIENT",
            first_name="Jane",
            last_name="Fallback",
        )

        response = authenticated_client.get(
            reverse("custom_admin:donor_search"),
            {
                "q": "Fallback",
                "campaign_id": str(campaign.id),
                "source": "data_file",
            },
        )

        payload = response.json()

        assert response.status_code == 200
        assert len(payload["donors"]) == 1
        assert payload["donors"][0]["source"] == "house_file"
        assert payload["donors"][0]["urn"] == "URN-HOUSE-FALLBACK"
        assert payload["donors"][0]["country"] == "Ireland"
        assert payload["donors"][0]["gift_aid_declaration"] is True
        assert payload["donors"][0]["no_thank_you"] is False

    def test_data_file_search_prefers_campaign_data_file_over_house_file(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        user = UserFactory()
        campaign = CampaignFactory(donor_source="data_file")
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="URN-DATA-FILE",
            first_name="Alice",
            last_name="Preferred",
            country="United Kingdom",
            gift_aid_declaration=True,
            no_thank_you=True,
        )
        DonorFactory(
            client=campaign.client,
            urn="URN-HOUSE-FILE",
            first_name="Alice",
            last_name="Preferred",
        )

        response = authenticated_client.get(
            reverse("custom_admin:donor_search"),
            {
                "q": "Preferred",
                "campaign_id": str(campaign.id),
                "source": "data_file",
            },
        )

        payload = response.json()

        assert response.status_code == 200
        assert len(payload["donors"]) == 1
        assert payload["donors"][0]["source"] == "data_file"
        assert payload["donors"][0]["urn"] == "URN-DATA-FILE"
        assert payload["donors"][0]["country"] == "United Kingdom"
        assert payload["donors"][0]["gift_aid_declaration"] is True
        assert payload["donors"][0]["no_thank_you"] is True
