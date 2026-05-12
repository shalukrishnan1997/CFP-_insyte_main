"""Regression tests for data-file upload API behavior."""

from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from campaigns.models import CampaignDataFile, DataFileUpload
from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db()
class TestDataFileUploadApiFallback:
    """Ensure uploads still work when background queuing is unavailable."""

    def test_create_provisions_missing_campaign_data_file(self) -> None:
        client = APIClient()
        user = UserFactory(is_staff=True, is_superuser=True)
        client.force_authenticate(user=user)

        campaign = CampaignFactory(created_by=user)
        upload_file = SimpleUploadedFile(
            "donors.csv",
            b"urn|first_name|last_name\nURN001|John|Smith\n",
            content_type="text/csv",
        )

        with patch("notifications.models.Notification.objects.create"):
            response = client.post(
                "/admin/api/data-file-uploads/",
                {"data_file": str(campaign.id), "file": upload_file},
                format="multipart",
            )

        assert response.status_code == 201
        assert CampaignDataFile.objects.filter(campaign=campaign).exists()

    def test_create_falls_back_to_synchronous_processing(self) -> None:
        client = APIClient()
        user = UserFactory(is_staff=True, is_superuser=True)
        client.force_authenticate(user=user)

        campaign = CampaignFactory(created_by=user)
        CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        upload_file = SimpleUploadedFile(
            "donors.csv",
            b"urn|first_name|last_name\nURN001|John|Smith\n",
            content_type="text/csv",
        )

        with (
            patch(
                "core.tasks.process_data_file_upload_task.delay",
                side_effect=Exception("broker down"),
            ),
            patch("notifications.models.Notification.objects.create"),
        ):
            response = client.post(
                "/admin/api/data-file-uploads/",
                {"data_file": str(campaign.id), "file": upload_file},
                format="multipart",
            )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "completed"
        assert body["successful_imports"] == 1
        assert body["failed_imports"] == 0

        upload = DataFileUpload.objects.get(id=body["id"])
        assert upload.status == "completed"
        assert upload.successful_imports == 1

    def test_manual_process_falls_back_to_synchronous_processing(self) -> None:
        client = APIClient()
        user = UserFactory(is_staff=True, is_superuser=True)
        client.force_authenticate(user=user)

        campaign = CampaignFactory(created_by=user)
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        upload = DataFileUpload.objects.create(
            data_file=data_file,
            uploaded_by=user,
            status="pending",
        )
        upload.file.save(
            "donors.csv",
            ContentFile(b"urn|first_name|last_name\nURN001|John|Smith\n"),
            save=True,
        )

        with (
            patch(
                "core.tasks.process_data_file_upload_task.delay",
                side_effect=Exception("broker down"),
            ),
            patch("notifications.models.Notification.objects.create"),
        ):
            response = client.post(f"/admin/api/data-file-uploads/{upload.id}/process/")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["successful_imports"] == 1
        assert body["failed_imports"] == 0

        upload.refresh_from_db()
        assert upload.status == "completed"
        assert upload.successful_imports == 1

    def test_create_allows_non_staff_user_with_system_access_group(self) -> None:
        client = APIClient()
        user = UserFactory(is_staff=False, is_superuser=False)
        user.groups.add(Group.objects.create(name="Operations"))
        client.force_authenticate(user=user)

        campaign = CampaignFactory(created_by=user)
        upload_file = SimpleUploadedFile(
            "donors.csv",
            b"urn|first_name|last_name\nURN001|John|Smith\n",
            content_type="text/csv",
        )

        with patch("notifications.models.Notification.objects.create"):
            response = client.post(
                "/admin/api/data-file-uploads/",
                {"data_file": str(campaign.id), "file": upload_file},
                format="multipart",
            )

        assert response.status_code == 201
