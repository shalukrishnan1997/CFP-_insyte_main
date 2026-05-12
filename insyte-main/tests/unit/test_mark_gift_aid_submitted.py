"""Tests for the ``mark_gift_aid_submitted`` view.

The view is the operator's signal that a Gift Aid CSV has been handed to
HMRC; ``DonationBatch.gift_aid_submitted_at`` then gates the
banking-reversal cascade's HMRC retraction warning.

Coverage:
    - Idempotency (re-clicking is a no-op).
    - Missing-CSV path returns an error message.
    - Audit log written on success.
    - Open-redirect guard: ``Referer`` is ignored; redirect always goes to
      the deterministic batch-review URL.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from donations.models import DonationBatch
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    UserFactory,
)


def _login_staff() -> tuple[Client, object]:
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


@pytest.mark.django_db()
class TestMarkGiftAidSubmitted:
    """Behavioural tests for ``mark_gift_aid_submitted``."""

    def test_marks_batch_as_submitted_and_redirects_to_batch_review(self) -> None:
        client, _user = _login_staff()
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
        )

        response = client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
        )

        assert response.status_code == 302
        assert response.url == reverse(
            "custom_admin:qa_batch_review", kwargs={"batch_id": batch.id}
        )
        batch.refresh_from_db()
        assert batch.gift_aid_submitted_at is not None

    def test_ignores_referer_header(self) -> None:
        """Open-redirect guard: Referer must not influence the redirect target.

        Even with CSRF protection, trusting Referer would let a phishing
        flow bounce a staff user off-site after a successful POST. The view
        always redirects to the deterministic batch-review URL.
        """
        client, _user = _login_staff()
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
        )

        response = client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
            HTTP_REFERER="https://evil.example.com/attack",
        )

        assert response.status_code == 302
        # Redirect target must be the in-app batch review URL, NOT the
        # attacker-controlled Referer.
        assert response.url == reverse(
            "custom_admin:qa_batch_review", kwargs={"batch_id": batch.id}
        )
        assert "evil.example.com" not in response.url

    def test_idempotent_on_already_submitted_batch(self) -> None:
        """Re-clicking the button after first submission is a no-op."""
        client, _user = _login_staff()
        campaign = CampaignFactory()
        first_submitted = timezone.now()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
            gift_aid_submitted_at=first_submitted,
        )

        response = client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
        )

        assert response.status_code == 302
        batch.refresh_from_db()
        # Original timestamp preserved — second click does not re-set.
        assert batch.gift_aid_submitted_at == first_submitted

    def test_missing_csv_returns_error_and_does_not_mark(self) -> None:
        """Batches without an auto-generated CSV cannot be marked submitted."""
        client, _user = _login_staff()
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="",
        )

        response = client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
        )

        assert response.status_code == 302
        batch.refresh_from_db()
        assert batch.gift_aid_submitted_at is None

    def test_audit_log_written_on_success(self) -> None:
        """Successful mark records an UPDATE audit row with the new timestamp."""
        client, _user = _login_staff()
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
        )

        client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
        )

        update_logs = AuditLog.objects.filter(
            model_name="DonationBatch",
            action="UPDATE",
            object_id=str(batch.id),
        )
        # At least one entry whose changes mention gift_aid_submitted_at.
        assert any(
            "gift_aid_submitted_at" in (log.changes or {}) for log in update_logs
        )

    def test_anonymous_user_blocked(self) -> None:
        """Without authentication, the view is not reachable.

        Defence-in-depth: the cascade depends on the marker being trustworthy,
        so the view must require an authenticated staff user (the
        ``@is_authenticated_and_is_staff`` decorator). An anon user must
        not be able to flip this state — the endpoint should redirect to
        login or 403, never 200.
        """
        client = Client()
        campaign = CampaignFactory()
        batch = DonationBatchFactory(
            campaign=campaign,
            status=DonationBatch.STATUS_APPROVED,
            gift_aid_report_path="gift_aid_reports/test.csv",
        )

        response = client.post(
            reverse(
                "custom_admin:mark_gift_aid_submitted", kwargs={"batch_id": batch.id}
            ),
        )

        assert response.status_code in (302, 403)
        # Confirm the marker did not move:
        batch.refresh_from_db()
        assert batch.gift_aid_submitted_at is None
        # If a redirect, it must point at the configured LOGIN_URL — not
        # the batch review (which would leak that the resource exists).
        # Using settings.LOGIN_URL keeps this test correct if the project
        # later moves login to a different route.
        if response.status_code == 302:
            assert response.headers["Location"].startswith(settings.LOGIN_URL)
