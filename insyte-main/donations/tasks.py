"""Celery tasks owned by the donations app."""

from typing import Any

from celery import shared_task
from celery.utils.log import get_task_logger
from django.utils import timezone

from donations.models import REVIEWER_LOCK_TTL, DonationBatch

logger = get_task_logger(__name__)


@shared_task(name="donations.release_stale_batch_locks")
def release_stale_batch_locks_task() -> dict[str, Any]:
    """Clear reviewer claims that have aged past :data:`REVIEWER_LOCK_TTL`.

    Runs periodically via ``CELERY_BEAT_SCHEDULE`` so an abandoned tab does
    not block the QA queue indefinitely. Active claims (within the TTL) are
    left untouched. Mirrors the same staleness check used by
    :func:`donations.models.DonationBatch.reviewer_lock_is_active`.

    Returns:
        Dict reporting the number of locks released.
    """
    threshold = timezone.now() - REVIEWER_LOCK_TTL
    released = DonationBatch.objects.filter(
        reviewer_locked_at__lt=threshold,
        reviewer_locked_by__isnull=False,
    ).update(
        reviewer_locked_by=None,
        reviewer_locked_at=None,
        updated_at=timezone.now(),
    )
    if released:
        logger.info("Released %s stale QA reviewer batch lock(s)", released)
    return {"released": released}
