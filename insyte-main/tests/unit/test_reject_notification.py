"""Tests for the QA-rejection ops-inbox notification (Basecamp todo #14).

Paul Nichols' 2026-02-27 ask: "rejected records should be emailed to a
generic email so that an operations person can deal with the record and
possibly create an issue letter if required." The trigger lives in
``custom_admin/views/qa_review.py``; the helper lives next to
``send_hgv_notification`` in ``custom_admin/views/utils.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from custom_admin.views.utils import send_rejection_notification
from donations.models import Donation
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
)

_QA_REQUIRED_FIELDS_PAYLOAD = {
    "donor_title": "Mr",
    "donor_first_name": "Test",
    "donor_last_name": "Donor",
    "package_code": "PKG",
    "donor_address_line1": "1 Test Street",
    "donor_postcode": "AB1 2CD",
}


@pytest.mark.django_db()
class TestSendRejectionNotificationHelper:
    """Direct unit coverage on the service helper."""

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_sends_email_with_donor_campaign_and_reason(self) -> None:
        client = ClientFactory(name="Acme Charity")
        campaign = CampaignFactory(name="Spring Appeal 2026", client=client)
        donor = DonorFactory(
            first_name="Jane",
            last_name="Doe",
            urn="URN-12345",
            client=client,
        )
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            donor=donor,
            amount=Decimal("42.50"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_ILLEGIBLE,
            qa_notes="Cannot read amount field",
        )

        send_rejection_notification(donation)

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["ops@test.local"]
        assert "Donation rejected" in message.subject
        assert "Jane Doe" in message.subject
        assert "Spring Appeal 2026" in message.subject
        body = message.body
        assert "Jane Doe" in body
        assert "URN-12345" in body
        assert "Acme Charity" in body
        assert "42.50" in body
        # Human-readable reject-reason label, not the choice key.
        assert "Illegible / cannot read form" in body
        assert "Cannot read amount field" in body
        # Admin link back to the donation.
        donation_path = reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch.id, str(donation.id)],
        )
        assert donation_path in body

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_includes_scan_placeholder_image_url_when_present(self) -> None:
        scan_batch = ScanBatchFactory()
        donation = DonationFactory(
            amount=Decimal("10.00"),
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_OTHER,
            qa_notes="See scan",
        )
        ScanPlaceholderFactory(
            batch=scan_batch,
            donation=donation,
            image_url="https://cdn.example.com/scans/page-42.jpg",
        )
        donation.refresh_from_db()

        send_rejection_notification(donation)

        assert len(mail.outbox) == 1
        assert "https://cdn.example.com/scans/page-42.jpg" in mail.outbox[0].body

    @override_settings(OPERATIONS_REJECT_EMAIL="")
    def test_skips_silently_when_setting_is_empty(self) -> None:
        donation = DonationFactory(
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_ILLEGIBLE,
        )

        send_rejection_notification(donation)

        assert mail.outbox == []

    @override_settings(OPERATIONS_REJECT_EMAIL="ops1@test.local, ops2@test.local")
    def test_supports_comma_separated_recipients(self) -> None:
        donation = DonationFactory(
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_DUPLICATE,
        )

        send_rejection_notification(donation)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ops1@test.local", "ops2@test.local"]

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_falls_back_to_no_urn_when_donor_unknown(self) -> None:
        donation = DonationFactory(
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_OTHER,
            qa_notes="Cold donation",
        )
        # Strip donor links so the URN-resolution chain falls through.
        donation.donor = None
        donation.data_file_donor = None
        donation.system_donor = None
        donation.save(
            update_fields=["donor", "data_file_donor", "system_donor", "updated_at"]
        )

        send_rejection_notification(donation)

        assert len(mail.outbox) == 1
        assert "no URN" in mail.outbox[0].body


@pytest.mark.django_db()
class TestQaRejectionTriggersOpsEmail:
    """End-to-end coverage via the QA review POST handler."""

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_per_donation_reject_sends_ops_email_and_marks_donation_rejected(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign)
        # Donor first/last are overwritten from the POST payload by the QA
        # handler's ``_apply_donation_edits_from_post`` step, so the email's
        # donor name is whatever ``_QA_REQUIRED_FIELDS_PAYLOAD`` carries
        # (Test / Donor) — not whatever the factory seeded.
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("75.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch.id, str(donation.id)],
        )

        response = authenticated_client.post(
            url,
            {
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_DAMAGED_FORM,
                "qa_notes": "Form torn in transit",
                **_QA_REQUIRED_FIELDS_PAYLOAD,
            },
        )

        assert response.status_code == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_REJECTED
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["ops@test.local"]
        assert "Donation rejected" in message.subject
        # Donor name in the subject reflects the edited POST values.
        assert "Test Donor" in message.subject
        assert "Damaged or incomplete form" in message.body
        assert "Form torn in transit" in message.body

    @override_settings(OPERATIONS_REJECT_EMAIL="")
    def test_per_donation_reject_skips_email_when_setting_unset(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("75.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch.id, str(donation.id)],
        )

        response = authenticated_client.post(
            url,
            {
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_DUPLICATE,
                "qa_notes": "Duplicate",
                **_QA_REQUIRED_FIELDS_PAYLOAD,
            },
        )

        assert response.status_code == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_REJECTED
        assert mail.outbox == []

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_email_send_failure_does_not_block_rejection(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("75.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch.id, str(donation.id)],
        )

        with patch(
            "custom_admin.views.utils.send_mail",
            side_effect=RuntimeError("SMTP unavailable"),
        ) as mocked_send_mail:
            response = authenticated_client.post(
                url,
                {
                    "action": "reject",
                    "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
                    "qa_notes": "Other reason",
                    **_QA_REQUIRED_FIELDS_PAYLOAD,
                },
            )

        # Rejection still committed even though the email send blew up.
        # Asserting send_mail was actually invoked guards against a future
        # refactor that silently moves the notification call out of the
        # reject branch.
        assert mocked_send_mail.call_count == 1
        assert response.status_code == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_REJECTED
        assert mail.outbox == []

    @override_settings(OPERATIONS_REJECT_EMAIL="ops@test.local")
    def test_approval_does_not_trigger_rejection_email(
        self,
        authenticated_client: Any,
        staff_user: Any,
    ) -> None:
        campaign = CampaignFactory(status="active")
        batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=batch,
            amount=Decimal("25.00"),
            payment_method="cheque",
            qa_status=Donation.QA_STATUS_PENDING,
        )

        from custom_admin.views import qa_review

        qa_review._claim_reviewer_lock(batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        url = reverse(
            "custom_admin:qa_single_donation_review",
            args=[batch.id, str(donation.id)],
        )

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
        # No outbound rejection email — there might be HGV mail in another
        # path, but at threshold 0 / unset that's unaffected by us.
        rejection_mail = [m for m in mail.outbox if "rejected" in m.subject.lower()]
        assert rejection_mail == []
