"""Unit tests for audit utility functions."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from audit.utils import (
    get_client_ip,
    log_action,
    log_bulk_create,
    log_bulk_delete,
    log_bulk_update,
    log_export,
    log_import,
    log_model_create,
    log_model_delete,
    log_model_update,
    log_request_action,
)
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestLogAction:
    """Tests for log_action function."""

    def test_creates_audit_log_with_user(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_action(user=user, action="CREATE", model_name="TestModel")
        assert AuditLog.objects.filter(user=user, action="CREATE").exists()

    def test_creates_audit_log_without_user(self) -> None:
        from audit.models import AuditLog

        log_action(user=None, action="DELETE", model_name="TestModel")
        assert AuditLog.objects.filter(user=None, action="DELETE").exists()

    def test_anonymous_user_stored_as_none(self) -> None:
        from audit.models import AuditLog

        anon = AnonymousUser()
        log_action(user=anon, action="READ", model_name="AnonModel")
        log = AuditLog.objects.filter(action="READ", model_name="AnonModel").first()
        assert log is not None
        assert log.user is None

    def test_stores_all_fields(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_action(
            user=user,
            action="UPDATE",
            model_name="Campaign",
            object_id="test-id-123",
            object_repr="Campaign: Test",
            changes={"name": ["old", "new"]},
            ip_address="127.0.0.1",
            user_agent="TestAgent/1.0",
            batch_id="batch-001",
            batch_size=5,
            summary="Updated campaign name",
        )
        log = AuditLog.objects.filter(action="UPDATE", model_name="Campaign").first()
        assert log is not None
        assert log.object_id == "test-id-123"
        assert log.ip_address == "127.0.0.1"
        assert log.batch_size == 5
        assert "Updated campaign name" in log.summary

    def test_truncates_long_object_repr(self) -> None:
        from audit.models import AuditLog

        long_repr = "x" * 600
        log_action(
            user=None, action="CREATE", model_name="BigModel", object_repr=long_repr
        )
        log = AuditLog.objects.filter(model_name="BigModel").first()
        assert log is not None
        assert len(log.object_repr) <= 500

    def test_none_object_id_stored_as_empty_string(self) -> None:
        from audit.models import AuditLog

        log_action(user=None, action="CREATE", model_name="NullId", object_id=None)
        log = AuditLog.objects.filter(model_name="NullId").first()
        assert log is not None
        assert log.object_id == ""


@pytest.mark.django_db()
class TestLogModelCreate:
    """Tests for log_model_create."""

    def test_logs_creation_with_model_instance(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        client = ClientFactory()
        log_model_create(instance=client, user=user)
        assert AuditLog.objects.filter(action="CREATE", model_name="Client").exists()

    def test_logs_creation_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.post("/test/")
        request.META["HTTP_USER_AGENT"] = "TestBrowser"
        user = UserFactory()
        request.user = user
        client = ClientFactory()
        log_model_create(instance=client, user=user, request=request)
        log = (
            AuditLog.objects.filter(action="CREATE", model_name="Client")
            .order_by("-created_at")
            .first()
        )
        assert log is not None
        assert log.user_agent == "TestBrowser"


@pytest.mark.django_db()
class TestLogModelUpdate:
    """Tests for log_model_update."""

    def test_logs_update_with_changes(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        campaign = CampaignFactory()
        changes = {"name": ["old_name", "new_name"]}
        log_model_update(instance=campaign, user=user, changes=changes)
        log = AuditLog.objects.filter(action="UPDATE", model_name="Campaign").first()
        assert log is not None
        assert "name" in log.summary

    def test_logs_update_without_changes(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        campaign = CampaignFactory()
        log_model_update(instance=campaign, user=user)
        assert AuditLog.objects.filter(action="UPDATE", model_name="Campaign").exists()

    def test_logs_update_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.put("/test/")
        request.META["REMOTE_ADDR"] = "192.168.1.1"
        user = UserFactory()
        campaign = CampaignFactory()
        log_model_update(instance=campaign, user=user, request=request)
        log = (
            AuditLog.objects.filter(action="UPDATE", model_name="Campaign")
            .order_by("-created_at")
            .first()
        )
        assert log is not None
        assert log.ip_address == "192.168.1.1"


@pytest.mark.django_db()
class TestLogModelDelete:
    """Tests for log_model_delete."""

    def test_logs_deletion(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        client = ClientFactory()
        obj_id = str(client.id)
        log_model_delete(instance=client, user=user)
        assert AuditLog.objects.filter(
            action="DELETE", model_name="Client", object_id=obj_id
        ).exists()

    def test_logs_deletion_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.delete("/test/")
        request.META["REMOTE_ADDR"] = "10.0.0.1"
        user = UserFactory()
        client = ClientFactory()
        log_model_delete(instance=client, user=user, request=request)
        log = (
            AuditLog.objects.filter(action="DELETE", model_name="Client")
            .order_by("-created_at")
            .first()
        )
        assert log is not None
        assert log.ip_address == "10.0.0.1"


@pytest.mark.django_db()
class TestLogBulkOperations:
    """Tests for log_bulk_create, log_bulk_update, log_bulk_delete."""

    def test_log_bulk_create(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_bulk_create(model_name="Donor", count=50, user=user, batch_id="batch-1")
        log = AuditLog.objects.filter(action="BULK_CREATE", model_name="Donor").first()
        assert log is not None
        assert log.batch_size == 50
        assert log.batch_id == "batch-1"

    def test_log_bulk_create_with_details(self) -> None:
        from audit.models import AuditLog

        log_bulk_create(model_name="Donor", count=10, details="from CSV import")
        log = (
            AuditLog.objects.filter(action="BULK_CREATE", model_name="Donor")
            .order_by("-created_at")
            .first()
        )
        assert log is not None
        assert "from CSV import" in log.summary

    def test_log_bulk_create_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.post("/import/")
        request.META["REMOTE_ADDR"] = "192.168.0.1"
        log_bulk_create(model_name="Campaign", count=3, request=request)
        log = (
            AuditLog.objects.filter(action="BULK_CREATE", model_name="Campaign")
            .order_by("-created_at")
            .first()
        )
        assert log is not None
        assert log.ip_address == "192.168.0.1"

    def test_log_bulk_update(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_bulk_update(model_name="Donation", count=100, user=user)
        log = AuditLog.objects.filter(
            action="BULK_UPDATE", model_name="Donation"
        ).first()
        assert log is not None
        assert log.batch_size == 100

    def test_log_bulk_update_with_details(self) -> None:
        from audit.models import AuditLog

        log_bulk_update(model_name="Invoice", count=5, details="status update")
        log = (
            AuditLog.objects.filter(action="BULK_UPDATE", model_name="Invoice")
            .order_by("-created_at")
            .first()
        )
        assert "status update" in log.summary

    def test_log_bulk_update_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.patch("/bulk/")
        request.META["REMOTE_ADDR"] = "10.1.2.3"
        log_bulk_update(model_name="Batch", count=2, request=request)
        log = (
            AuditLog.objects.filter(action="BULK_UPDATE", model_name="Batch")
            .order_by("-created_at")
            .first()
        )
        assert log.ip_address == "10.1.2.3"

    def test_log_bulk_delete(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_bulk_delete(model_name="OldRecord", count=25, user=user)
        log = AuditLog.objects.filter(
            action="BULK_DELETE", model_name="OldRecord"
        ).first()
        assert log is not None
        assert log.batch_size == 25

    def test_log_bulk_delete_with_details(self) -> None:
        from audit.models import AuditLog

        log_bulk_delete(model_name="Archive", count=7, details="annual purge")
        log = (
            AuditLog.objects.filter(action="BULK_DELETE", model_name="Archive")
            .order_by("-created_at")
            .first()
        )
        assert "annual purge" in log.summary

    def test_log_bulk_delete_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.delete("/bulk/")
        request.META["REMOTE_ADDR"] = "172.16.0.1"
        log_bulk_delete(model_name="Temp", count=3, request=request)
        log = (
            AuditLog.objects.filter(action="BULK_DELETE", model_name="Temp")
            .order_by("-created_at")
            .first()
        )
        assert log.ip_address == "172.16.0.1"


@pytest.mark.django_db()
class TestLogImportExport:
    """Tests for log_import and log_export."""

    def test_log_import(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_import(model_name="Donor", count=200, user=user, filename="donors.csv")
        log = AuditLog.objects.filter(action="IMPORT", model_name="Donor").first()
        assert log is not None
        assert "donors.csv" in log.summary
        assert log.batch_size == 200

    def test_log_import_without_filename(self) -> None:
        from audit.models import AuditLog

        log_import(model_name="Campaign", count=5)
        log = AuditLog.objects.filter(action="IMPORT", model_name="Campaign").first()
        assert log is not None

    def test_log_import_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.post("/import/")
        request.META["REMOTE_ADDR"] = "192.168.1.100"
        log_import(model_name="Donor", count=10, request=request)
        log = (
            AuditLog.objects.filter(action="IMPORT", model_name="Donor")
            .order_by("-created_at")
            .first()
        )
        assert log.ip_address == "192.168.1.100"

    def test_log_export(self) -> None:
        from audit.models import AuditLog

        user = UserFactory()
        log_export(model_name="Donation", count=500, user=user, export_type="CSV")
        log = AuditLog.objects.filter(action="EXPORT", model_name="Donation").first()
        assert log is not None
        assert "CSV" in log.summary

    def test_log_export_with_request(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.get("/export/")
        request.META["REMOTE_ADDR"] = "10.0.0.5"
        log_export(model_name="Invoice", count=10, request=request, export_type="PDF")
        log = (
            AuditLog.objects.filter(action="EXPORT", model_name="Invoice")
            .order_by("-created_at")
            .first()
        )
        assert log.ip_address == "10.0.0.5"


class TestGetClientIp:
    """Tests for get_client_ip utility function."""

    def test_returns_remote_addr(self) -> None:
        rf = RequestFactory()
        request = rf.get("/")
        request.META["REMOTE_ADDR"] = "1.2.3.4"
        assert get_client_ip(request) == "1.2.3.4"

    def test_prefers_x_forwarded_for(self) -> None:
        rf = RequestFactory()
        request = rf.get("/")
        request.META["HTTP_X_FORWARDED_FOR"] = "44.55.66.77, 10.0.0.1"
        request.META["REMOTE_ADDR"] = "1.2.3.4"
        assert get_client_ip(request) == "44.55.66.77"

    def test_strips_whitespace_from_x_forwarded_for(self) -> None:
        rf = RequestFactory()
        request = rf.get("/")
        request.META["HTTP_X_FORWARDED_FOR"] = "  99.88.77.66  , 10.0.0.1"
        assert get_client_ip(request) == "99.88.77.66"


@pytest.mark.django_db()
class TestLogRequestAction:
    """Tests for log_request_action."""

    def test_logs_request_action(self) -> None:
        from audit.models import AuditLog

        rf = RequestFactory()
        request = rf.get("/test/")
        request.META["REMOTE_ADDR"] = "9.8.7.6"
        user = UserFactory()
        request.user = user
        log_request_action(
            request=request,
            action="VIEW",
            model_name="Invoice",
            object_id="inv-001",
            summary="Viewed invoice",
        )
        log = AuditLog.objects.filter(action="VIEW", model_name="Invoice").first()
        assert log is not None
        assert log.ip_address == "9.8.7.6"
