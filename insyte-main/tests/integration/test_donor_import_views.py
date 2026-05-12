from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from django.urls import reverse

from campaigns.models import CampaignDataFile, DataFileUpload
from tests.factories import CampaignFactory, ClientFactory, DonorFactory


@pytest.mark.django_db()
class TestDonorImportViews:
    def test_donor_import_page_loads(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        response = authenticated_client.get(reverse("custom_admin:donor_imports"))

        assert response.status_code == 200
        assert b"Donor Imports" in response.content

    def test_donor_import_page_prefills_selected_entities(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory(name="Alpha Client")
        campaign = CampaignFactory(client=client_obj, name="Spring Appeal")

        response = authenticated_client.get(
            reverse("custom_admin:donor_imports"),
            {
                "import_type": "data_file",
                "client_id": str(client_obj.id),
                "campaign_id": str(campaign.id),
            },
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert "Alpha Client" in content
        assert "Spring Appeal" in content

    def test_house_file_import_runs_from_donor_import_page(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        client_obj = ClientFactory(name="Client A")
        old_donor = DonorFactory(
            client=client_obj,
            urn="OLD-URN",
            first_name="Legacy",
            last_name="Donor",
            postcode="LS1 1AA",
        )
        upload = SimpleUploadedFile(
            "house_file.csv",
            b"urn|first_name|last_name|postcode\nURN-CLIENT-A|John|Smith|LS1 4AB\n",
            content_type="text/csv",
        )

        response = authenticated_client.post(
            reverse("custom_admin:donor_imports"),
            {
                "import_type": "house_file",
                "client_id": str(client_obj.id),
                "house_file": upload,
            },
        )

        assert response.status_code == 200
        assert not type(old_donor).objects.filter(pk=old_donor.pk).exists()
        assert (
            type(old_donor)
            .objects.filter(client=client_obj, urn="URN-CLIENT-A")
            .exists()
        )
        assert (
            b"Replaced the house file for Client A with 1 donor(s)." in response.content
        )

    def test_client_edit_no_longer_shows_house_file_section(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory(name="Link Client")

        response = authenticated_client.get(
            reverse("custom_admin:client_edit", kwargs={"client_id": client_obj.id})
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert "House File" not in content
        assert "Manage House File Import" not in content
        assert "Open Reconciliation Workspace" not in content

    def test_campaign_edit_links_to_donor_import_page(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory(name="Link Client")
        campaign = CampaignFactory(client=client_obj)
        CampaignDataFile.objects.create(campaign=campaign)

        response = authenticated_client.get(
            reverse("custom_admin:campaign_edit", kwargs={"campaign_id": campaign.id})
        )

        assert response.status_code == 200
        assert (
            reverse("custom_admin:donor_imports")
            + f"?import_type=data_file&client_id={client_obj.id}&campaign_id={campaign.id}"
        ) in response.content.decode()

    def test_donor_import_page_shows_recent_data_file_uploads(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        client_obj = ClientFactory(name="History Client")
        campaign = CampaignFactory(client=client_obj, name="History Appeal")
        data_file = CampaignDataFile.objects.create(
            campaign=campaign, created_by=staff_user
        )
        DataFileUpload.objects.create(
            data_file=data_file,
            uploaded_by=staff_user,
            status="completed",
            total_rows=5,
            successful_imports=5,
            failed_imports=0,
        )

        response = authenticated_client.get(
            reverse("custom_admin:donor_imports"),
            {
                "import_type": "data_file",
                "client_id": str(client_obj.id),
                "campaign_id": str(campaign.id),
            },
        )

        assert response.status_code == 200
        assert b"Recent Data File Uploads" in response.content
        assert b"History Appeal" in response.content
