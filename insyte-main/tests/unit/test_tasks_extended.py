"""Tests for core/tasks.py Celery task functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from donors.models import Donor
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestSendBatchStatusEmail:
    """Tests for send_batch_status_email task."""

    @patch("django.core.mail.send_mail")
    def test_returns_success_on_email_sent(self, mock_send: MagicMock) -> None:
        from core.tasks import send_batch_status_email

        result = send_batch_status_email(
            "test@example.com",
            "Test Subject",
            "<html>Test</html>",
        )

        assert result["success"] is True
        mock_send.assert_called_once()

    @patch("django.core.mail.send_mail", side_effect=OSError("connection refused"))
    def test_transport_error_propagates_for_retry(self, mock_send: MagicMock) -> None:
        """Transport-shaped errors (``OSError``, ``SMTPException``) match
        ``autoretry_for``. In a running worker Celery wraps them as
        ``Retry``; in eager test mode the underlying exception re-raises
        instead. Either path proves the autoretry-eligible code path
        executed."""
        from celery.exceptions import Retry

        from core.tasks import send_batch_status_email

        with pytest.raises((OSError, Retry)):
            send_batch_status_email(
                "test@example.com",
                "Test Subject",
                "<html>Error</html>",
            )

        mock_send.assert_called_once()

    @patch("django.core.mail.send_mail", side_effect=ValueError("programming bug"))
    def test_does_not_retry_on_programming_error(self, mock_send: MagicMock) -> None:
        """Errors outside the narrow ``autoretry_for`` tuple propagate
        immediately so the task fails fast instead of burning 5 retries."""
        from core.tasks import send_batch_status_email

        with pytest.raises(ValueError, match="programming bug"):
            send_batch_status_email(
                "test@example.com",
                "Test Subject",
                "<html>Error</html>",
            )

        mock_send.assert_called_once()

    def test_autoretry_for_is_narrow(self) -> None:
        """Lock in the contract: only transport-shaped exceptions trigger
        retry. Without this guard, a future widening back to
        ``(Exception,)`` would silently re-introduce the
        retry-on-programming-bug regression."""
        import smtplib

        from core.tasks import send_batch_status_email

        autoretry = send_batch_status_email.autoretry_for
        assert OSError in autoretry
        assert smtplib.SMTPException in autoretry
        for forbidden in (Exception, NameError, TypeError, ValueError):
            assert forbidden not in autoretry, forbidden


@pytest.mark.django_db()
class TestProcessDataFileUploadTask:
    """Tests for process_data_file_upload_task."""

    def test_returns_error_when_upload_not_found(self) -> None:
        from core.tasks import process_data_file_upload_task

        result = process_data_file_upload_task("00000000-0000-0000-0000-000000000000")

        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("core.utils.process_data_file_upload", return_value=(5, 2, ["err1"]))
    def test_processes_upload_successfully(self, mock_process: MagicMock) -> None:
        from campaigns.models import CampaignDataFile, DataFileUpload
        from core.tasks import process_data_file_upload_task

        campaign = CampaignFactory()
        user = UserFactory(is_staff=True)
        data_file = CampaignDataFile.objects.create(campaign=campaign)
        upload = DataFileUpload.objects.create(
            data_file=data_file,
            uploaded_by=user,
            status="pending",
        )

        result = process_data_file_upload_task(str(upload.id))

        assert result["success"] is True
        assert result["successful_imports"] == 5
        assert result["failed_imports"] == 2
        assert result["total_errors"] == 1

    def test_returns_error_when_already_processing(self) -> None:
        from campaigns.models import CampaignDataFile, DataFileUpload
        from core.tasks import process_data_file_upload_task

        campaign = CampaignFactory()
        user = UserFactory(is_staff=True)
        data_file = CampaignDataFile.objects.create(campaign=campaign)
        upload = DataFileUpload.objects.create(
            data_file=data_file,
            uploaded_by=user,
            status="processing",
        )

        result = process_data_file_upload_task(str(upload.id))

        assert result["success"] is False
        assert "already" in result["error"]


@pytest.mark.django_db()
class TestCleanupOldUploads:
    """Tests for cleanup_old_uploads task."""

    def test_deletes_old_upload_records(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from campaigns.models import CampaignDataFile, DataFileUpload
        from core.tasks import cleanup_old_uploads

        campaign = CampaignFactory()
        user = UserFactory(is_staff=True)
        data_file = CampaignDataFile.objects.create(campaign=campaign)

        # Create a completed upload older than 30 days
        old_upload = DataFileUpload.objects.create(
            data_file=data_file, uploaded_by=user, status="completed"
        )
        DataFileUpload.objects.filter(pk=old_upload.pk).update(
            created_at=timezone.now() - timedelta(days=31)
        )

        result = cleanup_old_uploads()

        assert result["deleted_count"] >= 1

    def test_returns_zero_when_no_old_uploads(self) -> None:
        from core.tasks import cleanup_old_uploads

        result = cleanup_old_uploads()

        assert result["deleted_count"] == 0


@pytest.mark.django_db()
class TestAutoExportPendingDonorsTask:
    """Tests for auto_export_pending_donors_task."""

    def test_exports_separate_files_per_client_and_links_to_client_setup(
        self,
        tmp_path: object,
        settings: object,
    ) -> None:
        from core.tasks import auto_export_pending_donors_task
        from notifications.models import Notification

        settings.MEDIA_ROOT = str(tmp_path)  # type: ignore[attr-defined]
        staff_user = UserFactory(is_staff=True, is_active=True)
        client_a = CampaignFactory(client__client_code="AAA").client
        client_b = CampaignFactory(client__client_code="BBB").client
        DonorFactory(
            client=client_a,
            verification_status=Donor.VERIFICATION_PENDING_EXPORT,
            first_name="Alice",
            last_name="Alpha",
        )
        DonorFactory(
            client=client_b,
            verification_status=Donor.VERIFICATION_PENDING_EXPORT,
            first_name="Bob",
            last_name="Beta",
        )

        result = auto_export_pending_donors_task.run()

        notification = Notification.objects.get(user=staff_user)
        exported_files = [Path(tmp_path) / path for path in result["filepaths"]]

        assert result["success"] is True
        assert result["pending_count"] == 2
        assert result["client_count"] == 2
        assert len(result["filepaths"]) == 2
        assert notification.title == "Pending Donor Export Ready"
        assert "Donor Imports (House File)" in notification.message
        assert "Pending Export" in notification.message
        assert "2 client file(s)" in notification.message
        assert notification.link == "/admin/clients/"
        assert any(
            path.name.startswith("pending_donors_AAA_") for path in exported_files
        )
        assert any(
            path.name.startswith("pending_donors_BBB_") for path in exported_files
        )
        assert all(path.exists() for path in exported_files)

        aaa_export = next(path for path in exported_files if "AAA" in path.name)
        bbb_export = next(path for path in exported_files if "BBB" in path.name)
        assert "Alice,Alpha" in aaa_export.read_text(encoding="utf-8")
        assert "Bob,Beta" not in aaa_export.read_text(encoding="utf-8")
        assert "Bob,Beta" in bbb_export.read_text(encoding="utf-8")
        assert "Alice,Alpha" not in bbb_export.read_text(encoding="utf-8")


@pytest.mark.django_db()
class TestUpdateDataFileStatistics:
    """Tests for update_data_file_statistics task."""

    def test_returns_error_when_not_found(self) -> None:
        from core.tasks import update_data_file_statistics

        result = update_data_file_statistics("00000000-0000-0000-0000-000000000000")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_updates_donor_count(self) -> None:
        from campaigns.models import CampaignDataFile
        from core.tasks import update_data_file_statistics

        campaign = CampaignFactory()
        CampaignDataFile.objects.create(campaign=campaign)

        result = update_data_file_statistics(str(campaign.id))

        assert result["success"] is True
        assert "total_donors" in result


@pytest.mark.django_db()
class TestBulkDeleteDataFileDonorsTask:
    """Tests for bulk_delete_data_file_donors_task."""

    def test_returns_error_when_data_file_not_found(self) -> None:
        from core.tasks import bulk_delete_data_file_donors_task

        result = bulk_delete_data_file_donors_task(
            "00000000-0000-0000-0000-000000000000"
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_deletes_donors_and_returns_count(self) -> None:
        from campaigns.models import CampaignDataFile
        from core.tasks import bulk_delete_data_file_donors_task

        campaign = CampaignFactory()
        CampaignDataFile.objects.create(campaign=campaign)

        result = bulk_delete_data_file_donors_task(str(campaign.id))

        assert result["success"] is True
        assert result["deleted_count"] == 0


@pytest.mark.django_db()
class TestSendDonationReceipt:
    """Tests for send_donation_receipt task."""

    def test_returns_error_when_donation_not_found(self) -> None:
        from core.tasks import send_donation_receipt

        result = send_donation_receipt(
            "00000000-0000-0000-0000-000000000000",
            "00000000-0000-0000-0000-000000000001",
        )

        assert result["success"] is False

    @patch("django.core.mail.EmailMessage.send")
    @patch("django.template.loader.render_to_string")
    def test_sends_receipt_for_donation_with_donor(
        self, mock_render: MagicMock, mock_send: MagicMock
    ) -> None:
        from core.tasks import send_donation_receipt

        mock_render.return_value = "<html>Receipt</html>"
        campaign = CampaignFactory()
        customer = StripeCustomerFactory(client=campaign.client)
        donation = DonationFactory(campaign=campaign, payment_status="completed")
        payment = StripePaymentFactory(
            stripe_customer=customer, donation=donation, status="succeeded"
        )

        result = send_donation_receipt(str(donation.id), str(payment.id))

        assert result["success"] is True


@pytest.mark.django_db()
class TestRetryFailedPayments:
    """Tests for retry_failed_payments task."""

    def test_returns_success_with_zero_retries_when_none_pending(self) -> None:
        from core.tasks import retry_failed_payments

        result = retry_failed_payments()

        assert result["success"] is True
        assert result["attempted"] == 0

    @patch("payments.services.StripePaymentService.retry_failed_payment")
    def test_retries_eligible_payments(self, mock_retry: MagicMock) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from core.tasks import retry_failed_payments

        mock_retry.return_value = {"status": "retry_scheduled"}

        customer = StripeCustomerFactory()
        StripePaymentFactory(
            stripe_customer=customer,
            status="failed",
            retry_count=0,
            max_retries=3,
            next_retry_at=timezone.now() - timedelta(hours=1),
        )

        result = retry_failed_payments()

        assert result["success"] is True
        assert result["attempted"] >= 1


@pytest.mark.django_db()
class TestOnBatchApprovedTask:
    """Tests for on_batch_approved_task."""

    def test_returns_error_when_batch_not_found(self) -> None:
        from core.tasks import on_batch_approved_task

        result = on_batch_approved_task(99999)

        assert result["success"] is False

    @patch("core.tasks._run_gift_aid_for_batch")
    def test_runs_gift_aid_step(self, mock_gift_aid: MagicMock) -> None:
        from core.tasks import on_batch_approved_task

        mock_gift_aid.return_value = {"success": True, "rows": 0}
        batch = DonationBatchFactory(status="approved")

        result = on_batch_approved_task(batch.pk)

        assert result["success"] is True
        assert result["steps"]["gift_aid"] == {"success": True, "rows": 0}
        mock_gift_aid.assert_called_once()
