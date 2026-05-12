"""Additional tests for core/utils.py — process_data_file_upload and related helpers."""

from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import InMemoryUploadedFile

from campaigns.models import CampaignDataFile, DataFileUpload
from core.utils import parse_excel_file, process_data_file_upload
from donors.models import DataFileDonor
from tests.factories import CampaignFactory, UserFactory

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_csv_upload(content: str, filename: str = "donors.csv") -> DataFileUpload:
    """Create a real DataFileUpload instance backed by test models."""
    user = UserFactory()
    campaign = CampaignFactory(created_by=user)
    data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
    return DataFileUpload.objects.create(
        data_file=data_file,
        file=ContentFile(content.encode("utf-8"), name=filename),
        uploaded_by=user,
        status="pending",
    )


# ──────────────────────────────────────────────────────────────────────────────
# parse_excel_file — edge cases
# ──────────────────────────────────────────────────────────────────────────────


class TestParseExcelFileEdgeCases:
    """Edge cases not covered in test_utils_extended.py."""

    def test_raises_value_error_when_no_active_sheet(self) -> None:
        pytest.importorskip("openpyxl")
        with (
            patch("core.utils.openpyxl") as mock_openpyxl,
            patch("core.utils.HAS_OPENPYXL", True),
        ):
            mock_wb = MagicMock()
            mock_wb.active = None
            mock_openpyxl.load_workbook.return_value = mock_wb

            fake_file = MagicMock(spec=InMemoryUploadedFile)
            with pytest.raises(ValueError, match="no active sheet"):
                parse_excel_file(fake_file)


# ──────────────────────────────────────────────────────────────────────────────
# process_data_file_upload
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db()
class TestProcessDataFileUpload:
    """Tests for process_data_file_upload() utility."""

    def test_unsupported_file_format_returns_error(self) -> None:
        upload = MagicMock()
        upload.file.name = "donors.pdf"
        result = process_data_file_upload(upload)
        assert result == (
            0,
            0,
            ["Unsupported file format. Please upload a pipe-delimited .csv file."],
        )
        assert upload.status == "failed"

    def test_missing_required_columns_returns_error(self) -> None:
        csv_content = "email|address\nfoo@bar.com|123 Street\n"
        upload = _make_csv_upload(csv_content)
        result = process_data_file_upload(upload)
        success, _failed, errors = result
        assert success == 0
        assert errors  # should have column mismatch error
        assert upload.status == "failed"

    def test_comma_delimited_csv_returns_error(self) -> None:
        csv_content = "urn,first_name,last_name\nURN001,John,Smith\n"
        upload = _make_csv_upload(csv_content)
        result = process_data_file_upload(upload)
        assert result == (
            0,
            0,
            [
                "Invalid delimiter. Only pipe-delimited CSV files are supported. Use '|' between columns."
            ],
        )
        assert upload.status == "failed"

    def test_empty_csv_completes_with_zero_rows(self) -> None:
        csv_content = "urn|first_name|last_name\n"
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, _errors = result
        assert success == 0
        assert failed == 0

    def test_valid_csv_imports_donors(self) -> None:
        csv_content = (
            "urn|first_name|last_name|email|phone|postcode\n"
            "URN001|John|Smith|j@ex.com|07123456789|SW1A1AA\n"
            "URN002|Jane|Doe|j2@ex.com|07987654321|EH11AA\n"
        )
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, errors = result
        assert success == 2
        assert failed == 0
        assert errors == []
        assert DataFileDonor.objects.filter(data_file=upload.data_file).count() == 2

    def test_row_missing_urn_counts_as_failed(self) -> None:
        csv_content = "urn|first_name|last_name\n|John|Smith\n"
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, errors = result
        assert failed == 1
        assert success == 0
        assert any("URN" in e for e in errors)
        assert DataFileDonor.objects.filter(data_file=upload.data_file).count() == 0

    def test_row_missing_first_name_counts_as_failed(self) -> None:
        csv_content = "urn|first_name|last_name\nURN001||Smith\n"
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        _success, failed, _errors = result
        assert failed == 1

    def test_row_with_optional_fields(self) -> None:
        csv_content = (
            "urn|first_name|last_name|email|package_code\n"
            "URN001|John|Smith|john.smith@example.org|PKG-01\n"
        )
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, _errors = result
        assert success == 1
        assert failed == 0
        donor = DataFileDonor.objects.get(data_file=upload.data_file, urn="URN001")
        assert donor.package_code == "PKG-01"
        assert donor.email == "john.smith@example.org"

    def test_exception_during_row_processing_handled(self) -> None:
        csv_content = "urn|first_name|last_name\nURN001|John|Smith\n"
        upload = _make_csv_upload(csv_content)
        with (
            patch("donors.models.DataFileDonor", side_effect=Exception("DB error")),
            patch("notifications.models.Notification.objects.create"),
        ):
            result = process_data_file_upload(upload)
        success, failed, _errors = result
        assert failed == 1
        assert success == 0

    def test_partial_import_creates_warning_notification(self) -> None:
        """Replacement imports reject mixed-valid uploads before changing rows."""
        csv_content = (
            "urn|first_name|last_name\n"
            "URN001|John|Smith\n"
            "|Bad|Row\n"  # missing URN → failed
        )
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, _errors = result
        assert success == 0
        assert failed == 1
        assert DataFileDonor.objects.filter(data_file=upload.data_file).count() == 0

    def test_all_failed_creates_error_notification(self) -> None:
        """Test that all-failed creates error notification."""
        csv_content = "urn|first_name|last_name\n|Bad|Row\n"  # missing URN
        upload = _make_csv_upload(csv_content)
        with patch("notifications.models.Notification.objects.create"):
            result = process_data_file_upload(upload)
        success, failed, _errors = result
        assert failed == 1
        assert success == 0

    def test_exception_at_top_level_returns_failed(self) -> None:
        """Test that a top-level exception returns failed status."""
        upload = MagicMock()
        upload.file.name = "donors.csv"
        upload.file.read.side_effect = OSError("Read error")
        result = process_data_file_upload(upload)
        assert result[0] == 0
        assert upload.status == "failed"
