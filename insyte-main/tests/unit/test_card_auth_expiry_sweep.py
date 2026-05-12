"""Phase 2 unit tests for ``payments.tasks.sweep_expiring_card_auths``."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from payments.models import StripePayment
from payments.tasks import sweep_expiring_card_auths
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)


def _make_requires_capture_payment(*, age_days: int = 6, amount: str = "25.00"):
    """Seed a Donation + StripePayment in requires_capture state.

    Backdates ``created_at`` on both rows by ``age_days`` so the sweep
    threshold (6 days) can be exercised either side of the boundary.
    """
    user = UserFactory(is_staff=True)
    batch = DonationBatchFactory(created_by=user)
    donation = DonationFactory(
        campaign=batch.campaign,
        batch=batch,
        payment_method="card",
        payment_status="requires_capture",
        qa_status="pending",
        amount=Decimal(amount),
        filled_by=user,
    )
    customer = StripeCustomerFactory(client=donation.campaign.client)
    payment = StripePayment.objects.create(
        stripe_payment_intent_id=f"pi_sweep_{donation.id}",
        stripe_customer=customer,
        donation=donation,
        amount=donation.amount,
        currency=donation.currency,
        status=StripePayment.STATUS_REQUIRES_CAPTURE,
        description="sweep test",
        metadata={"moto": "true"},
        processed_by=user,
    )
    backdate_to = timezone.now() - timedelta(days=age_days)
    StripePayment.objects.filter(pk=payment.pk).update(created_at=backdate_to)
    return donation, payment, user


@pytest.mark.django_db()
class TestSweepExpiringCardAuths:
    """The hourly auth-expiry beat task."""

    def test_no_expiring_auths_is_noop(self) -> None:
        result = sweep_expiring_card_auths()
        assert result == {"checked": 0, "notified": 0}

    def test_recent_auth_below_threshold_not_swept(self) -> None:
        from notifications.models import Notification

        _make_requires_capture_payment(age_days=2)
        result = sweep_expiring_card_auths()
        assert result["checked"] == 0
        assert Notification.objects.count() == 0

    def test_old_auth_past_threshold_notifies_creator(self) -> None:
        from notifications.models import Notification

        donation, _, user = _make_requires_capture_payment(age_days=6, amount="42.00")

        result = sweep_expiring_card_auths()

        assert result["checked"] == 1
        assert result["notified"] >= 1
        notifications = list(Notification.objects.filter(user=user))
        assert len(notifications) == 1
        n = notifications[0]
        assert n.title == "MOTO authorisation expiring"
        assert "42.00" in n.message
        assert str(donation.id) in n.message
        assert n.notification_type == Notification.TYPE_WARNING

    def test_idempotent_within_same_day(self) -> None:
        """Two sweeps in the same day must not double-notify the same donor."""
        from notifications.models import Notification

        _make_requires_capture_payment(age_days=6)

        sweep_expiring_card_auths()
        sweep_expiring_card_auths()

        assert Notification.objects.count() == 1

    def test_succeeded_payment_not_swept(self) -> None:
        _donation, payment, _ = _make_requires_capture_payment(age_days=10)
        payment.status = StripePayment.STATUS_SUCCEEDED
        payment.save(update_fields=["status"])

        result = sweep_expiring_card_auths()

        assert result["checked"] == 0
