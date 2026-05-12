"""Tests for the Campaign archive + cascade-protect behavior (audit §4.1).

Five FK relationships from Campaign were ``on_delete=CASCADE`` before
2026-05-02; a single accidental campaign delete would have wiped all
linked donations, batches, letter templates, letter batches, and scan
batches in one transaction. They are now ``PROTECT`` and operators must
``Campaign.archive()`` instead of deleting.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError

from letters.models import LetterTemplate
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    ScanBatchFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestCampaignArchive:
    def test_archive_sets_flag_and_timestamp(self) -> None:
        campaign = CampaignFactory()
        assert campaign.is_archived is False
        assert campaign.archived_at is None

        campaign.archive()
        campaign.refresh_from_db()

        assert campaign.is_archived is True
        assert campaign.archived_at is not None

    def test_archive_is_idempotent(self) -> None:
        campaign = CampaignFactory()
        campaign.archive()
        first_ts = campaign.archived_at

        campaign.archive()  # second call must not change the timestamp
        campaign.refresh_from_db()
        assert campaign.archived_at == first_ts

    def test_unarchive_clears_flag_and_timestamp(self) -> None:
        campaign = CampaignFactory()
        campaign.archive()

        campaign.unarchive()
        campaign.refresh_from_db()
        assert campaign.is_archived is False
        assert campaign.archived_at is None


@pytest.mark.django_db()
class TestCampaignProtectFromDeletion:
    def test_delete_with_donation_raises_protected_error(self) -> None:
        donation = DonationFactory()
        with pytest.raises(ProtectedError):
            donation.campaign.delete()

    def test_delete_with_donation_batch_raises_protected_error(self) -> None:
        batch = DonationBatchFactory()
        with pytest.raises(ProtectedError):
            batch.campaign.delete()

    def test_delete_with_scan_batch_raises_protected_error(self) -> None:
        scan = ScanBatchFactory()
        with pytest.raises(ProtectedError):
            scan.campaign.delete()

    def test_delete_with_letter_template_raises_protected_error(self) -> None:
        user = UserFactory()
        campaign = CampaignFactory(created_by=user)
        LetterTemplate.objects.create(
            campaign=campaign,
            file=SimpleUploadedFile("t.docx", b"x"),
            created_by=user,
        )
        with pytest.raises(ProtectedError):
            campaign.delete()

    def test_delete_with_no_linked_rows_succeeds(self) -> None:
        """PROTECT only triggers when there are linked rows — empty campaigns
        can still be removed by an operator with the right permission."""
        campaign = CampaignFactory()
        campaign_id = campaign.pk
        campaign.delete()

        from campaigns.models import Campaign

        assert not Campaign.objects.filter(pk=campaign_id).exists()
