"""Unit tests for core.signals — campaign, batch, and notification signals."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestHandleCampaignStatusChange:
    """Tests for handle_campaign_status_change signal."""

    def test_closing_campaign_deletes_data_file(self) -> None:
        from campaigns.models import Campaign, CampaignDataFile

        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)
        data_file = CampaignDataFile.objects.create(
            campaign=campaign,
            total_donors=0,
        )
        assert CampaignDataFile.objects.filter(pk=data_file.pk).exists()

        campaign.status = Campaign.STATUS_CLOSED
        campaign.save()

        assert not CampaignDataFile.objects.filter(pk=data_file.pk).exists()

    def test_non_close_status_change_keeps_data_file(self) -> None:
        from campaigns.models import Campaign, CampaignDataFile

        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)
        data_file = CampaignDataFile.objects.create(
            campaign=campaign,
            total_donors=0,
        )

        campaign.status = Campaign.STATUS_ACTIVE
        campaign.save()

        assert CampaignDataFile.objects.filter(pk=data_file.pk).exists()

    def test_closing_campaign_without_data_file_is_safe(self) -> None:
        from campaigns.models import Campaign

        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)
        # No data file created
        campaign.status = Campaign.STATUS_CLOSED
        # Should not raise
        campaign.save()

    def test_new_campaign_no_signal_fired(self) -> None:
        """For a brand new instance (no pk), signal skips processing."""
        from campaigns.models import Campaign

        # CampaignFactory.create already sets status; just verify it saves fine
        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)
        assert campaign.pk is not None

    def test_new_campaign_does_not_log_missing_prior_row_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """First-save campaigns should not emit a bogus DoesNotExist signal error."""
        from campaigns.models import Campaign

        caplog.set_level(logging.ERROR, logger="core.signals")

        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)

        assert campaign.pk is not None
        assert not any(
            "Error in handle_campaign_status_change" in message
            for message in caplog.messages
        )


@pytest.mark.django_db()
class TestCaptureAndNotifyBatchStatusChange:
    """Tests for capture_batch_old_status + notify_batch_status_change signals."""

    def test_batch_approved_creates_notification(self) -> None:
        from donations.models import DonationBatch
        from notifications.models import Notification

        reviewer = UserFactory(is_staff=True)
        creator = UserFactory()
        batch = DonationBatchFactory(
            created_by=creator,
            reviewed_by=reviewer,
            status=DonationBatch.STATUS_PENDING_QA,
        )

        batch.status = DonationBatch.STATUS_APPROVED
        batch.reviewed_by = reviewer
        batch.save(update_fields=["status", "reviewed_by"])

        assert Notification.objects.filter(
            user=creator,
            notification_type="success",
        ).exists()

    def test_batch_rejected_creates_notification(self) -> None:
        from donations.models import DonationBatch
        from notifications.models import Notification

        reviewer = UserFactory(is_staff=True)
        creator = UserFactory()
        batch = DonationBatchFactory(
            created_by=creator,
            reviewed_by=reviewer,
            status=DonationBatch.STATUS_PENDING_QA,
        )

        batch.status = DonationBatch.STATUS_REJECTED
        batch.review_notes = "Too many errors"
        batch.save(update_fields=["status", "review_notes"])

        assert Notification.objects.filter(
            user=creator,
            notification_type="error",
        ).exists()

    def test_batch_pending_qa_creates_notification(self) -> None:
        from donations.models import DonationBatch
        from notifications.models import Notification

        creator = UserFactory()
        reviewer = UserFactory(is_staff=True)
        batch = DonationBatchFactory(
            created_by=creator,
            reviewed_by=reviewer,
            status=DonationBatch.STATUS_APPROVED,
        )

        batch.status = DonationBatch.STATUS_PENDING_QA
        batch.save(update_fields=["status"])

        assert Notification.objects.filter(
            user=creator,
            notification_type="warning",
        ).exists()

    def test_no_notification_for_same_status(self) -> None:
        from donations.models import DonationBatch
        from notifications.models import Notification

        creator = UserFactory()
        batch = DonationBatchFactory(
            created_by=creator,
            status=DonationBatch.STATUS_PENDING_QA,
        )
        initial_count = Notification.objects.filter(user=creator).count()

        # Save without changing status (no update_fields mentioning status)
        batch.batch_name = "Updated name"
        batch.save(update_fields=["batch_name"])

        assert Notification.objects.filter(user=creator).count() == initial_count

    def test_no_notification_when_no_creator(self) -> None:
        from donations.models import DonationBatch
        from notifications.models import Notification

        batch = DonationBatchFactory(
            created_by=None,
            status=DonationBatch.STATUS_PENDING_QA,
        )
        initial_count = Notification.objects.count()

        batch.status = DonationBatch.STATUS_APPROVED
        batch.save(update_fields=["status"])

        # Notification count should stay same (no creator to notify)
        assert Notification.objects.count() == initial_count

    def test_batch_created_does_not_send_notification(self) -> None:
        """Creation (created=True) should skip all notification logic."""
        from donations.models import DonationBatch
        from notifications.models import Notification

        creator = UserFactory()
        initial_count = Notification.objects.filter(user=creator).count()
        # Creating a new batch fires post_save with created=True
        DonationBatchFactory(
            created_by=creator,
            status=DonationBatch.STATUS_PENDING_QA,
        )
        # No notification should be created from the create event alone
        # (status change notifications only trigger on updates)
        # The count should be the same
        # Note: the signal explicitly returns early for created=True
        assert Notification.objects.filter(user=creator).count() == initial_count

    @pytest.mark.django_db(transaction=True)
    @patch("core.tasks.send_batch_status_email")
    def test_email_sent_on_status_change(self, mock_task: MagicMock) -> None:
        # Uses transaction=True so the transaction.on_commit hooks scheduled
        # by notify_batch_status_change actually fire — under the default
        # @pytest.mark.django_db (no transaction) the wrapping atomic block
        # never commits and on_commit callbacks are dropped.
        from donations.models import DonationBatch

        creator = UserFactory(email="creator@example.com")
        reviewer = UserFactory(is_staff=True)
        batch = DonationBatchFactory(
            created_by=creator,
            reviewed_by=reviewer,
            status=DonationBatch.STATUS_PENDING_QA,
        )

        batch.status = DonationBatch.STATUS_APPROVED
        batch.save(update_fields=["status"])

        mock_task.delay.assert_called_once()


@pytest.mark.django_db()
class TestUpdateCampaignOnBatchSave:
    """Tests for update_campaign_on_batch_save and update_campaign_on_batch_delete signals."""

    def test_campaign_updated_when_batch_saved(self) -> None:
        from campaigns.models import Campaign

        batch = DonationBatchFactory()
        campaign = batch.campaign

        # Just verify no exception raised and campaign is still accessible
        assert Campaign.objects.filter(pk=campaign.pk).exists()

    def test_campaign_updated_when_batch_deleted(self) -> None:
        from campaigns.models import Campaign

        batch = DonationBatchFactory()
        campaign_pk = batch.campaign.pk

        batch.delete()

        assert Campaign.objects.filter(pk=campaign_pk).exists()
