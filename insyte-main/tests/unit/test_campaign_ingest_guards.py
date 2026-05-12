"""Tests for ``campaigns.ingest_guards.campaign_scan_block_reason``.

The historical bug — fixed by Item 2 of the 2026-05-05 audit — was that
only the ``/webhooks/scan-upload/`` endpoint enforced the
"campaign-must-be-active" rule. The admin "Pending R2 Folders" flow,
the ``/admin/api/scan-processing/create/`` API, and the folder-watcher
all happily created scan batches against draft / closed campaigns or
deactivated clients. The guard is the single source of truth used by
all four entry points.
"""

import json
from typing import Any

import pytest
from django.test import Client as DjangoClient

from campaigns.ingest_guards import campaign_scan_block_reason
from campaigns.models import Campaign
from tests.factories import CampaignFactory, ClientFactory


@pytest.mark.django_db()
class TestCampaignScanBlockReason:
    """Pure-function tests for the guard."""

    def test_active_campaign_with_active_client_returns_none(self) -> None:
        client = ClientFactory(is_active=True)
        campaign = CampaignFactory(client=client, status=Campaign.STATUS_ACTIVE)

        assert campaign_scan_block_reason(campaign) is None

    def test_none_campaign_returns_not_found(self) -> None:
        assert campaign_scan_block_reason(None) == "Campaign not found."

    def test_draft_campaign_is_blocked(self) -> None:
        campaign = CampaignFactory(status=Campaign.STATUS_DRAFT)

        reason = campaign_scan_block_reason(campaign)

        assert reason is not None
        assert "not active" in reason.lower()
        assert "Draft" in reason  # Uses get_status_display

    def test_closed_campaign_is_blocked(self) -> None:
        campaign = CampaignFactory(status=Campaign.STATUS_CLOSED)

        reason = campaign_scan_block_reason(campaign)

        assert reason is not None
        assert "not active" in reason.lower()

    def test_inactive_client_blocks_active_campaign(self) -> None:
        client = ClientFactory(is_active=False)
        campaign = CampaignFactory(client=client, status=Campaign.STATUS_ACTIVE)

        reason = campaign_scan_block_reason(campaign)

        assert reason is not None
        assert "deactivated" in reason.lower()
        assert client.name in reason


@pytest.mark.django_db()
class TestScanBatchCreateApiBlocksInactiveCampaign:
    """Cover ``scans/api_views.py:scan_batch_create`` — the JSON API."""

    def test_inactive_campaign_returns_400(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(status=Campaign.STATUS_DRAFT)

        response = authenticated_client.post(
            "/admin/api/scan-processing/create/",
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "r2_prefix": "ScanOutput/CFP/SPRING25/cheque/",
                    "payment_method": "cheque",
                    "scan_form_type": "simplex_with_payment",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        body = response.json()
        assert "not active" in body["error"].lower()

    def test_deactivated_client_returns_400(
        self,
        authenticated_client: DjangoClient,
        staff_user: Any,
    ) -> None:
        client = ClientFactory(is_active=False)
        campaign = CampaignFactory(client=client, status=Campaign.STATUS_ACTIVE)

        response = authenticated_client.post(
            "/admin/api/scan-processing/create/",
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "r2_prefix": "ScanOutput/CFP/SPRING25/cheque/",
                    "payment_method": "cheque",
                    "scan_form_type": "simplex_with_payment",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        body = response.json()
        assert "deactivated" in body["error"].lower()


@pytest.mark.django_db()
class TestResolveCampaignR2Service:
    """Cover ``scans/scan_processing_r2.py:_resolve_campaign``.

    This is the helper called from ``create_scan_batch_from_r2`` —
    so blocking here also blocks the Celery-task path (``scans/tasks.py
    :create_scan_batch_from_r2_task``).
    """

    def test_inactive_campaign_raises_value_error(self) -> None:
        from scans.scan_processing_r2 import _resolve_campaign

        campaign = CampaignFactory(status=Campaign.STATUS_CLOSED)

        with pytest.raises(ValueError, match="not active"):
            _resolve_campaign(str(campaign.id))

    def test_active_campaign_returns_instance(self) -> None:
        from scans.scan_processing_r2 import _resolve_campaign

        campaign = CampaignFactory(status=Campaign.STATUS_ACTIVE)

        resolved = _resolve_campaign(str(campaign.id))
        assert resolved.id == campaign.id


@pytest.mark.django_db()
class TestIngestFolderRefusesInactiveCampaign:
    """Cover ``scans/scan_folder.py:ScanFolderWatcherService.ingest_folder``.

    The folder-watcher pre-validates so the cron job stays idempotent
    even when an admin closes a campaign mid-watch cycle.
    """

    def test_ingest_folder_returns_error_for_closed_campaign(self) -> None:
        from scans.scan_folder import ScanFolderWatcherService

        # Use an explicit appeal_code so _resolve_campaign matches it.
        campaign = CampaignFactory(
            appeal_code="SPRING25",
            status=Campaign.STATUS_CLOSED,
        )

        result = ScanFolderWatcherService.ingest_folder(
            appeal_code=campaign.appeal_code,
            payment_method="cheque",
            scan_form_type="simplex_with_payment",
            r2_prefix=f"ScanOutput/{campaign.client.client_code}/SPRING25/cheque/",
        )

        assert result["status"] == "error"
        assert "not active" in result["error"].lower()
