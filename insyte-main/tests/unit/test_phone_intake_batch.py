"""Unit tests for ``donations.intake.create_phone_intake_batch``."""

from datetime import UTC, datetime

import pytest

from donations.intake import (
    PHONE_BATCH_NAME_PREFIX,
    create_phone_intake_batch,
)
from donations.models import DonationBatch
from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db()
class TestCreatePhoneIntakeBatch:
    """One batch per phone call — no reuse."""

    def test_creates_new_batch_with_timestamped_name(self) -> None:
        operator = UserFactory(username="alice")
        campaign = CampaignFactory(name="Spring 2026")
        now = datetime(2026, 5, 2, 14, 30, 25, 123456, tzinfo=UTC)

        batch = create_phone_intake_batch(operator=operator, campaign=campaign, now=now)

        assert batch.batch_name.startswith(PHONE_BATCH_NAME_PREFIX)
        assert "alice" in batch.batch_name
        assert "Spring 2026" in batch.batch_name
        # Microsecond-precision ISO timestamp is in the name.
        assert "2026-05-02T14:30:25.123456" in batch.batch_name
        assert batch.campaign == campaign
        assert batch.created_by == operator
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_each_call_creates_a_new_batch(self) -> None:
        """Two consecutive calls from the same operator+campaign+day get distinct batches."""
        operator = UserFactory(username="alice")
        campaign = CampaignFactory(name="Spring 2026")
        first_call = datetime(2026, 5, 2, 9, 0, 0, 1, tzinfo=UTC)
        second_call = datetime(2026, 5, 2, 9, 0, 0, 2, tzinfo=UTC)

        first = create_phone_intake_batch(
            operator=operator, campaign=campaign, now=first_call
        )
        second = create_phone_intake_batch(
            operator=operator, campaign=campaign, now=second_call
        )

        assert first.id != second.id
        assert first.batch_name != second.batch_name

    def test_different_operators_get_different_batches(self) -> None:
        alice = UserFactory(username="alice")
        bob = UserFactory(username="bob")
        campaign = CampaignFactory(name="Spring 2026")
        now = datetime(2026, 5, 2, 9, 0, 0, tzinfo=UTC)

        alice_batch = create_phone_intake_batch(
            operator=alice, campaign=campaign, now=now
        )
        bob_batch = create_phone_intake_batch(operator=bob, campaign=campaign, now=now)

        assert alice_batch.id != bob_batch.id
        assert "alice" in alice_batch.batch_name
        assert "bob" in bob_batch.batch_name

    def test_different_campaigns_get_different_batches(self) -> None:
        operator = UserFactory(username="alice")
        spring = CampaignFactory(name="Spring 2026")
        autumn = CampaignFactory(name="Autumn 2026")
        now = datetime(2026, 5, 2, 9, 0, 0, tzinfo=UTC)

        spring_batch = create_phone_intake_batch(
            operator=operator, campaign=spring, now=now
        )
        autumn_batch = create_phone_intake_batch(
            operator=operator, campaign=autumn, now=now
        )

        assert spring_batch.id != autumn_batch.id

    def test_now_defaults_to_current_time_when_not_supplied(self) -> None:
        operator = UserFactory(username="alice")
        campaign = CampaignFactory(name="Spring 2026")

        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        # Just verify it created something with a valid timestamp-shaped name.
        assert batch.batch_name.startswith(PHONE_BATCH_NAME_PREFIX)
        assert batch.status == DonationBatch.STATUS_PENDING_QA
