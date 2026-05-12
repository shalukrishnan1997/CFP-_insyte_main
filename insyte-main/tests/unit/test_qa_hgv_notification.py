"""Tests for the HGV-on-QA-approval hook (Item 4).

Until this fix, ``send_hgv_notification`` was only wired into
``save_donation_entry`` (now dead code) — neither scanned donations nor
phone-intake donations ever tripped the alert because both flows reach
the live ``Donation.objects.create`` paths in ``donations/intake.py``
and the OCR pipeline, which don't call it. The new hook in
``custom_admin/views/qa_review.py`` fires the alert at the QA-approval
transition, covering every donation regardless of intake path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from custom_admin.views.qa_review import (
    _fire_hgv_notification_for_approval,
    _fire_hgv_notifications_for_cascade,
)
from donations.models import Donation
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
)


@pytest.mark.django_db()
class TestFireHgvNotificationForApproval:
    """Direct unit coverage on the helper."""

    def test_calls_send_hgv_notification_with_donation_and_campaign(self) -> None:
        campaign = CampaignFactory(
            hgv_amount=Decimal("100.00"),
            campaign_manager_emails="manager@charity.test",
        )
        donation = DonationFactory(
            campaign=campaign, amount=Decimal("250.00"), payment_method="cheque"
        )

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            _fire_hgv_notification_for_approval(donation)

        mock_send.assert_called_once()
        called_donation, called_campaign = mock_send.call_args.args
        assert called_donation.pk == donation.pk
        assert called_campaign.pk == campaign.pk

    def test_swallows_exceptions(self) -> None:
        # Resend / SMTP outage must not roll back the QA approval.
        donation = DonationFactory(amount=Decimal("250.00"), payment_method="cheque")

        with patch(
            "custom_admin.views.qa_review.send_hgv_notification",
            side_effect=RuntimeError("Resend unavailable"),
        ):
            # No raise — the helper logs and continues.
            _fire_hgv_notification_for_approval(donation)

    def test_skips_when_campaign_missing(self) -> None:
        donation = DonationFactory(amount=Decimal("250.00"), payment_method="cheque")
        donation.campaign = None  # type: ignore[assignment]

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            _fire_hgv_notification_for_approval(donation)

        mock_send.assert_not_called()


@pytest.mark.django_db()
class TestFireHgvNotificationsForCascade:
    """Cover the bulk variant used by the batch-approve cascade."""

    def test_dispatches_one_notification_per_donation(self) -> None:
        campaign = CampaignFactory(
            hgv_amount=Decimal("100.00"),
            campaign_manager_emails="manager@charity.test",
        )
        batch = DonationBatchFactory(campaign=campaign)
        donations = [
            DonationFactory(
                campaign=campaign,
                batch=batch,
                amount=Decimal("250.00"),
                payment_method="cheque",
            )
            for _ in range(3)
        ]
        ids = [d.id for d in donations]

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            _fire_hgv_notifications_for_cascade(ids)

        assert mock_send.call_count == 3

    def test_empty_list_is_a_noop(self) -> None:
        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            _fire_hgv_notifications_for_cascade([])

        mock_send.assert_not_called()


_QA_REQUIRED_FIELDS_PAYLOAD = {
    "donor_title": "Mr",
    "donor_first_name": "Test",
    "donor_last_name": "Donor",
    "package_code": "PKG",
    "donor_address_line1": "1 Test Street",
    "donor_postcode": "AB1 2CD",
}


@pytest.mark.django_db()
class TestQaApprovalTriggersHgvEmail:
    """End-to-end: posting an ``approve`` action sends the HGV email."""

    def test_approving_above_threshold_donation_sends_hgv(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(
            hgv_amount=Decimal("100.00"),
            campaign_manager_emails="manager@charity.test",
            status="active",
        )
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("500.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from django.urls import reverse

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review", args=[batch.id, str(donation.id)]
        )

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            response = authenticated_client.post(
                url,
                {
                    "action": "approve",
                    "qa_notes": "",
                    **_QA_REQUIRED_FIELDS_PAYLOAD,
                },
            )

        assert response.status_code == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        mock_send.assert_called_once()

    def test_re_approving_already_approved_donation_does_not_resend(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(
            hgv_amount=Decimal("100.00"),
            campaign_manager_emails="manager@charity.test",
            status="active",
        )
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("500.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        from django.urls import reverse

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review", args=[batch.id, str(donation.id)]
        )

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            response = authenticated_client.post(
                url,
                {
                    "action": "approve",
                    "qa_notes": "",
                    **_QA_REQUIRED_FIELDS_PAYLOAD,
                },
            )

        assert response.status_code == 302
        # Idempotency: same final status, no duplicate email.
        mock_send.assert_not_called()

    def test_rejecting_does_not_send_hgv(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(
            hgv_amount=Decimal("100.00"),
            campaign_manager_emails="manager@charity.test",
            status="active",
        )
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("500.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from django.urls import reverse

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review", args=[batch.id, str(donation.id)]
        )

        with patch("custom_admin.views.qa_review.send_hgv_notification") as mock_send:
            response = authenticated_client.post(
                url,
                {
                    "action": "reject",
                    "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
                    "qa_notes": "Test rejection",
                    **_QA_REQUIRED_FIELDS_PAYLOAD,
                },
            )

        assert response.status_code == 302
        mock_send.assert_not_called()
