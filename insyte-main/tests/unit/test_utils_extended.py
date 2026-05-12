"""Unit tests for core.utils — utility functions."""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.test import RequestFactory

from tests.factories import UserFactory


class TestSanitizeError:
    """Tests for sanitize_error()."""

    def test_validation_error_returns_message(self) -> None:
        from django.core.exceptions import ValidationError

        from core.utils import sanitize_error

        exc = ValidationError("This field is required.")
        result = sanitize_error(exc)
        assert "required" in result

    def test_drf_validation_error_returns_message(self) -> None:
        from rest_framework.exceptions import ValidationError

        from core.utils import sanitize_error

        exc = ValidationError({"field": ["This field is required."]})
        result = sanitize_error(exc)
        assert isinstance(result, str)

    def test_integrity_error_unique_urn(self) -> None:
        from django.db import IntegrityError

        from core.utils import sanitize_error

        exc = IntegrityError("unique_urn_per_datafile")
        result = sanitize_error(exc)
        assert "URN" in result

    def test_integrity_error_generic(self) -> None:
        from django.db import IntegrityError

        from core.utils import sanitize_error

        exc = IntegrityError("UNIQUE constraint failed: core_donor.email")
        result = sanitize_error(exc)
        assert "integrity" in result.lower()

    def test_missing_in_message_preserves_message(self) -> None:
        from core.utils import sanitize_error

        exc = RuntimeError("Missing required field: first_name")
        result = sanitize_error(exc)
        assert "Missing required field" in result

    def test_generic_error_returns_safe_message(self) -> None:
        from core.utils import sanitize_error

        exc = RuntimeError("SELECT * FROM users WHERE id = 1; DROP TABLE users;")
        result = sanitize_error(exc)
        # Should not leak SQL
        assert "DROP TABLE" not in result
        assert "unexpected error" in result.lower()


class TestParseCsvFile:
    """Tests for parse_csv_file()."""

    def _make_csv_upload(self, content: str) -> InMemoryUploadedFile:
        data = content.encode("utf-8")
        return InMemoryUploadedFile(
            io.BytesIO(data), "file", "donors.csv", "text/csv", len(data), None
        )

    def test_parses_basic_csv(self) -> None:
        from core.utils import parse_csv_file

        csv_content = "urn|first_name|last_name\nURN001|John|Smith\nURN002|Jane|Doe"
        upload = self._make_csv_upload(csv_content)
        rows = parse_csv_file(upload)

        assert len(rows) == 2
        assert rows[0]["urn"] == "URN001"
        assert rows[0]["first_name"] == "John"
        assert rows[1]["last_name"] == "Doe"

    def test_strips_whitespace_from_values(self) -> None:
        from core.utils import parse_csv_file

        csv_content = "urn|first_name|last_name\n  URN001  |  John  |  Smith  "
        upload = self._make_csv_upload(csv_content)
        rows = parse_csv_file(upload)

        assert rows[0]["urn"] == "URN001"
        assert rows[0]["first_name"] == "John"

    def test_handles_bom(self) -> None:
        from core.utils import parse_csv_file

        # utf-8-sig encoding automatically prepends BOM bytes
        csv_content = "urn|first_name|last_name\nURN001|John|Smith"
        data = csv_content.encode("utf-8-sig")  # adds BOM prefix
        upload = InMemoryUploadedFile(
            io.BytesIO(data), "file", "donors.csv", "text/csv", len(data), None
        )
        rows = parse_csv_file(upload)

        assert rows[0]["urn"] == "URN001"

    def test_empty_csv_returns_empty_list(self) -> None:
        from core.utils import parse_csv_file

        csv_content = "urn|first_name|last_name\n"
        upload = self._make_csv_upload(csv_content)
        rows = parse_csv_file(upload)

        assert rows == []

    def test_empty_values_returned_as_empty_string(self) -> None:
        from core.utils import parse_csv_file

        csv_content = "urn|first_name|last_name|email\nURN001|John|Smith|"
        upload = self._make_csv_upload(csv_content)
        rows = parse_csv_file(upload)

        assert rows[0]["email"] == ""

    def test_rewinds_file_before_parsing(self) -> None:
        from core.utils import parse_csv_file

        csv_content = "urn|first_name|last_name\nURN001|John|Smith"
        upload = self._make_csv_upload(csv_content)
        upload.read(7)

        rows = parse_csv_file(upload)

        assert rows[0]["urn"] == "URN001"


class TestRestoreSessionFilters:
    """Tests for restore_session_filters()."""

    def test_returns_none_when_filter_keys_in_get(self, rf: RequestFactory) -> None:
        from core.utils import restore_session_filters

        request = rf.get("/test/?status=active")
        request.session = {"donor_filters": {"status": "active"}}  # type: ignore[assignment]
        result = restore_session_filters(
            request, "donor_filters", "core:donors", filter_keys=("status",)
        )
        assert result is None

    def test_redirects_when_session_has_filters(self, rf: RequestFactory) -> None:
        from core.utils import restore_session_filters

        request = rf.get("/test/")  # No GET params → filter_keys check is False
        request.session = {"donor_filters": {"status": "active"}}  # type: ignore[assignment]

        # Mock reverse so we don't need a real URL named "core:donors"
        with patch("core.utils.reverse", return_value="/donors/"):
            result = restore_session_filters(
                request, "donor_filters", "core:donors", filter_keys=("status",)
            )
        assert result is not None
        assert result.status_code == 302

    def test_returns_none_when_no_session_and_no_defaults(
        self, rf: RequestFactory
    ) -> None:
        from core.utils import restore_session_filters

        request = rf.get("/test/")
        request.session = {}  # type: ignore[assignment]
        with patch("core.utils.reverse", return_value="/donors/"):
            result = restore_session_filters(request, "donor_filters", "core:donors")
        assert result is None

    def test_redirects_with_default_params_when_nothing_else(
        self, rf: RequestFactory
    ) -> None:
        from core.utils import restore_session_filters

        request = rf.get("/test/")
        request.session = {}  # type: ignore[assignment]

        with patch("core.utils.reverse", return_value="/donors/"):
            result = restore_session_filters(
                request,
                "donor_filters",
                "core:donors",
                default_params={"status": "active"},
            )
        assert result is not None
        assert result.status_code == 302

    def test_no_redirect_when_filter_keys_match_get_params(
        self, rf: RequestFactory
    ) -> None:
        from core.utils import restore_session_filters

        request = rf.get("/test/?campaign=1")
        request.session = {"campaign_filters": {"campaign": "2"}}  # type: ignore[assignment]
        result = restore_session_filters(
            request,
            "campaign_filters",
            "core:campaigns",
            filter_keys=("campaign",),
        )
        assert result is None


@pytest.mark.django_db()
class TestLogAudit:
    """Tests for log_audit()."""

    def test_creates_audit_log_entry(self) -> None:
        from audit.models import AuditLog
        from core.utils import log_audit

        user = UserFactory()
        log_audit(
            user=user,
            action="CREATE",
            model_name="Donation",
            object_id="abc-123",
            object_repr="Donation(abc-123)",
        )
        assert AuditLog.objects.filter(user=user, action="CREATE").exists()

    def test_includes_ip_from_x_forwarded_for(self, rf: RequestFactory) -> None:
        from audit.models import AuditLog
        from core.utils import log_audit

        user = UserFactory()
        request = rf.get("/test/", HTTP_X_FORWARDED_FOR="10.0.0.1, 10.0.0.2")

        log_audit(
            user=user,
            action="UPDATE",
            model_name="Donor",
            request=request,
        )
        entry = AuditLog.objects.filter(user=user, action="UPDATE").first()
        assert entry is not None
        assert entry.ip_address == "10.0.0.1"

    def test_includes_ip_from_remote_addr(self, rf: RequestFactory) -> None:
        from audit.models import AuditLog
        from core.utils import log_audit

        user = UserFactory()
        request = rf.get("/test/", REMOTE_ADDR="192.168.1.100")

        log_audit(
            user=user,
            action="DELETE",
            model_name="Campaign",
            request=request,
        )
        entry = AuditLog.objects.filter(user=user, action="DELETE").first()
        assert entry is not None
        assert entry.ip_address == "192.168.1.100"

    def test_truncates_long_summary(self) -> None:
        from audit.models import AuditLog
        from core.utils import log_audit

        user = UserFactory()
        long_summary = "A" * 2000
        log_audit(
            user=user,
            action="BULK_UPDATE",
            model_name="Donation",
            summary=long_summary,
        )
        entry = AuditLog.objects.filter(user=user, action="BULK_UPDATE").first()
        assert entry is not None
        assert len(entry.summary) <= 1000

    def test_none_object_id_stored_as_empty(self) -> None:
        from audit.models import AuditLog
        from core.utils import log_audit

        user = UserFactory()
        log_audit(
            user=user,
            action="VIEW",
            model_name="Donor",
            object_id=None,
        )
        entry = AuditLog.objects.filter(user=user, action="VIEW").first()
        assert entry is not None
        assert entry.object_id == ""


@pytest.mark.django_db()
class TestLogBulkOperation:
    """Tests for log_bulk_operation()."""

    def test_creates_audit_entry_with_batch_info(self) -> None:
        from audit.models import AuditLog
        from core.utils import log_bulk_operation

        user = UserFactory()
        log_bulk_operation(
            user=user,
            action="BULK_DELETE",
            model_name="Donation",
            batch_id="batch-abc-123",
            count=50,
        )
        entry = AuditLog.objects.filter(user=user, action="BULK_DELETE").first()
        assert entry is not None
        assert entry.batch_size == 50

    def test_default_summary_generated_when_not_provided(self) -> None:
        from audit.models import AuditLog
        from core.utils import log_bulk_operation

        user = UserFactory()
        log_bulk_operation(
            user=user,
            action="BULK_IMPORT",
            model_name="Donor",
            batch_id="batch-xyz",
            count=25,
        )
        entry = AuditLog.objects.filter(user=user, action="BULK_IMPORT").first()
        assert entry is not None
        assert "25" in entry.summary
        assert "Donor" in entry.summary


class TestParseExcelFile:
    """Tests for parse_excel_file() — requires openpyxl."""

    def test_raises_import_error_when_openpyxl_missing(self) -> None:
        from core.utils import parse_excel_file

        with patch("core.utils.HAS_OPENPYXL", False):
            fake_file = MagicMock(spec=InMemoryUploadedFile)
            with pytest.raises(ImportError, match="openpyxl"):
                parse_excel_file(fake_file)

    def test_parses_excel_file(self) -> None:
        pytest.importorskip("openpyxl")
        import openpyxl

        from core.utils import parse_excel_file

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["urn", "first_name", "last_name"])  # type: ignore[arg-type]
        ws.append(["URN001", "John", "Smith"])  # type: ignore[arg-type]
        ws.append(["URN002", "Jane", "Doe"])  # type: ignore[arg-type]

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        upload = InMemoryUploadedFile(
            buf,
            "file",
            "donors.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            buf.getbuffer().nbytes,
            None,
        )
        rows = parse_excel_file(upload)

        assert len(rows) == 2
        assert rows[0]["urn"] == "URN001"
        assert rows[1]["first_name"] == "Jane"
