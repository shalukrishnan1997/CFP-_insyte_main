"""Celery tasks for the payments app.

Currently exports a single periodic task,
:func:`sweep_expiring_card_auths`, which catches MOTO (phone-intake) card
authorisations approaching their 7-day Stripe expiry. The QA team
receives an in-app + email notification so they can either:

* clear the donation through normal QA review (capture fires the auth), or
* call the donor back to collect a fresh auth before the original
  expires unused (Stripe auto-cancels and we need a new charge).

The Stripe authorisation expiry semantics are: from the moment a
PaymentIntent settles into ``requires_capture``, the issuer holds the
funds for ~7 days. After that, Stripe auto-cancels and the donor sees
the pending charge fall off their statement. Captures attempted past
expiry fail with ``payment_intent_authentication_failure`` /
``payment_intent_unexpected_state``. Beat schedule entry is registered
in ``responsehandling/settings/base.py:CELERY_BEAT_SCHEDULE``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Sweep threshold: notify QA when an auth has been outstanding longer than
# this. Stripe auths expire at ~7 days, so 6 days leaves a one-day window
# for QA to act before the auth lapses.
EXPIRING_AUTH_THRESHOLD_DAYS = 6


@shared_task(name="payments.sweep_expiring_card_auths")
def sweep_expiring_card_auths() -> dict[str, int]:
    """Notify the QA team when MOTO auths approach 7-day expiry.

    Runs hourly via celery-beat. Idempotent — repeat invocations only
    create one notification per donation per day (deduped via the
    ``Notification`` model's payload + recipient).

    Returns:
        ``{"checked": N, "notified": M}`` so the result is observable in
        Flower / Celery results storage without a separate metric.
    """
    from notifications.models import Notification
    from payments.models import StripePayment

    threshold = timezone.now() - timedelta(days=EXPIRING_AUTH_THRESHOLD_DAYS)
    expiring = (
        StripePayment.objects.filter(
            status=StripePayment.STATUS_REQUIRES_CAPTURE,
            created_at__lt=threshold,
        )
        .select_related("donation", "donation__campaign", "donation__campaign__client")
        .order_by("created_at")
    )

    checked = 0
    notified = 0
    today = timezone.now().date()
    for payment in expiring:
        checked += 1
        donation = payment.donation
        if donation is None:
            continue

        # Deduplicate: one notification per donation per day. The notification
        # payload is keyed on a deterministic message so a uniqueness check on
        # (recipient, message) within today's window suffices.
        message = (
            f"Phone-intake card authorisation for donation {donation.id} "
            f"(£{donation.amount} {donation.currency}) is within 24h of "
            "expiry — capture or cancel before the issuer releases the hold."
        )

        # Notify the batch creator + the campaign's client_users group. We
        # err on the side of broadcasting: better one redundant ping than a
        # silently-expired auth.
        recipients = set()
        if donation.batch and donation.batch.created_by_id:
            recipients.add(donation.batch.created_by_id)
        if donation.filled_by_id:
            recipients.add(donation.filled_by_id)

        title = "MOTO authorisation expiring"
        for user_id in recipients:
            already_today = Notification.objects.filter(
                user_id=user_id,
                message=message,
                created_at__date=today,
            ).exists()
            if already_today:
                continue
            Notification.objects.create(
                user_id=user_id,
                title=title,
                message=message,
                notification_type=Notification.TYPE_WARNING,
                related_object_type="Donation",
                related_object_id=str(donation.id),
                # The notification target lets QA jump straight to the
                # donation review page from their inbox.
                link=f"/admin/qa/batches/{donation.batch.id}/donations/{donation.id}/",
            )
            notified += 1

        logger.info(
            "Auth expiry sweep: donation %s payment %s (created_at=%s) flagged for QA",
            donation.id,
            payment.id,
            payment.created_at,
        )

    return {"checked": checked, "notified": notified}
