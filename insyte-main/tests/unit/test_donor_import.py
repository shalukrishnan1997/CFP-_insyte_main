import pytest
from django.core.files.base import ContentFile

from campaigns.models import CampaignDataFile, DataFileUpload
from core.utils import process_data_file_upload
from donors.models import DataFileDonor
from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db
class TestDonorImportReplacement:
    def test_donor_import_replaces_existing_campaign_rows(self):
        # Setup
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        # 1. First upload: One donor
        csv_content = (
            "urn|first_name|last_name|email|phone|city|package_code\n"
            "D001|John|Doe|john@example.com|07700900111|London|PKG1"
        )
        file = ContentFile(csv_content.encode("utf-8"), name="test.csv")
        upload1 = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success1, failed1, _ = process_data_file_upload(upload1)

        assert success1 == 1
        assert failed1 == 0
        assert (
            DataFileDonor.objects.filter(data_file=data_file, urn="D001").count() == 1
        )
        donor = DataFileDonor.objects.get(data_file=data_file, urn="D001")
        assert donor.first_name == "John"
        assert donor.email == "john@example.com"
        assert donor.phone == "07700900111"
        assert donor.city == "London"
        assert donor.package_code == "PKG1"

        # 2. Second upload: Replace existing campaign rows
        csv_content2 = (
            "urn|first_name|last_name|email|phone|city|package_code\n"
            "D001|John|Smith|smith@example.com|07700900222|Manchester|PKG2"
        )
        file2 = ContentFile(csv_content2.encode("utf-8"), name="test2.csv")
        upload2 = DataFileUpload.objects.create(
            data_file=data_file, file=file2, uploaded_by=user, status="pending"
        )

        success2, failed2, _ = process_data_file_upload(upload2)

        assert success2 == 1
        assert failed2 == 0
        assert (
            DataFileDonor.objects.filter(data_file=data_file, urn="D001").count() == 1
        )
        assert not DataFileDonor.objects.filter(pk=donor.pk).exists()
        donor = DataFileDonor.objects.get(data_file=data_file, urn="D001")
        assert donor.last_name == "Smith"
        assert donor.email == "smith@example.com"
        assert donor.phone == "07700900222"
        assert donor.city == "Manchester"
        assert donor.package_code == "PKG2"

    def test_donor_import_removes_rows_missing_from_latest_upload(self):
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        initial_csv = "urn|first_name|last_name\nD001|John|Doe\nD002|Jane|Doe\n"
        upload1 = DataFileUpload.objects.create(
            data_file=data_file,
            file=ContentFile(initial_csv.encode("utf-8"), name="initial.csv"),
            uploaded_by=user,
            status="pending",
        )
        process_data_file_upload(upload1)

        replacement_csv = "urn|first_name|last_name\nD001|John|Doe\n"
        upload2 = DataFileUpload.objects.create(
            data_file=data_file,
            file=ContentFile(replacement_csv.encode("utf-8"), name="replacement.csv"),
            uploaded_by=user,
            status="pending",
        )

        success, failed, errs = process_data_file_upload(upload2)

        assert success == 1
        assert failed == 0
        assert errs == []
        assert DataFileDonor.objects.filter(data_file=data_file, urn="D001").exists()
        assert not DataFileDonor.objects.filter(
            data_file=data_file, urn="D002"
        ).exists()

    def test_donor_import_missing_required_columns(self):
        # Setup
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        # Missing first_name
        csv_content = "urn|last_name\nD001|Doe"
        file = ContentFile(csv_content.encode("utf-8"), name="test_missing.csv")
        upload = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success, failed, errs = process_data_file_upload(upload)

        assert success == 0
        assert failed == 0
        assert "Missing required columns: First Name" in errs[0]

    def test_donor_import_rejects_duplicate_urns_in_same_upload(self):
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        csv_content = "urn|first_name|last_name\nD001|John|Doe\nD001|Jane|Doe"
        file = ContentFile(csv_content.encode("utf-8"), name="test_dup.csv")
        upload = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success, failed, errs = process_data_file_upload(upload)

        assert success == 0
        assert failed == 1
        assert errs == ["Row 2 (D001): Duplicate URN in upload"]
        assert DataFileDonor.objects.filter(data_file=data_file).count() == 0

    def test_donor_import_campaign_isolation(self):
        # Setup
        user = UserFactory()
        campaign1 = CampaignFactory(name="Campaign A")
        campaign2 = CampaignFactory(name="Campaign B")

        df1 = CampaignDataFile.objects.create(campaign=campaign1, created_by=user)
        df2 = CampaignDataFile.objects.create(campaign=campaign2, created_by=user)

        # Upload to Campaign A
        csv1 = "urn|first_name|last_name\nD001|John|Doe"
        file1 = ContentFile(csv1.encode("utf-8"), name="a.csv")
        upload1 = DataFileUpload.objects.create(
            data_file=df1, file=file1, uploaded_by=user
        )
        process_data_file_upload(upload1)

        # Upload to Campaign B with SAME URN
        csv2 = "urn|first_name|last_name\nD001|Jane|Smith"
        file2 = ContentFile(csv2.encode("utf-8"), name="b.csv")
        upload2 = DataFileUpload.objects.create(
            data_file=df2, file=file2, uploaded_by=user
        )
        process_data_file_upload(upload2)

        # Verify isolation: Both should exist separately
        assert DataFileDonor.objects.filter(data_file=df1, urn="D001").count() == 1
        assert DataFileDonor.objects.filter(data_file=df2, urn="D001").count() == 1

        donor1 = DataFileDonor.objects.get(data_file=df1, urn="D001")
        donor2 = DataFileDonor.objects.get(data_file=df2, urn="D001")

        assert donor1.last_name == "Doe"
        assert donor2.last_name == "Smith"

    def test_donor_import_rejects_comma_delimited_csv(self):
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        csv_content = "urn,first_name,last_name\nD001,John,Doe"
        file = ContentFile(csv_content.encode("utf-8"), name="comma.csv")
        upload = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success, failed, errs = process_data_file_upload(upload)

        assert success == 0
        assert failed == 0
        assert errs == [
            "Invalid delimiter. Only pipe-delimited CSV files are supported. Use '|' between columns."
        ]

    def test_donor_import_rejects_excel_uploads(self):
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        file = ContentFile(b"fake-excel-content", name="donors.xlsx")
        upload = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success, failed, errs = process_data_file_upload(upload)

        assert success == 0
        assert failed == 0
        assert errs == [
            "Unsupported file format. Please upload a pipe-delimited .csv file."
        ]

    def test_donor_import_ignores_removed_consent_columns(self):
        user = UserFactory()
        campaign = CampaignFactory()
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)

        csv_content = (
            "urn|first_name|last_name|gift_aid_declaration|no_thank_you|"
            "opt_in_email|opt_in_sms|opt_in_phone|opt_in_post\n"
            "D001|John|Doe|yes|yes|true|true|true|true"
        )
        file = ContentFile(csv_content.encode("utf-8"), name="test_removed_cols.csv")
        upload = DataFileUpload.objects.create(
            data_file=data_file, file=file, uploaded_by=user, status="pending"
        )

        success, failed, errs = process_data_file_upload(upload)

        assert success == 1
        assert failed == 0
        assert errs == []

        donor = DataFileDonor.objects.get(data_file=data_file, urn="D001")
        assert donor.gift_aid_declaration is False
        assert donor.no_thank_you is False
        assert donor.opt_in_email is False
        assert donor.opt_in_sms is False
        assert donor.opt_in_phone is False
        assert donor.opt_in_post is False
