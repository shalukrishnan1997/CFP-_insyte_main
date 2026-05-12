"""
Celery tasks for core app
Background tasks for time-consuming operations
"""

import random
import smtplib
from typing import Any

from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


def _is_lock_timeout_or_deadlock(exc: BaseException) -> bool:
    """Return True when *exc* represents a Postgres lock-timeout / deadlock.

    Two concurrent Stripe webhook deliveries for the same payment can both
    enter a ``select_for_update()`` block; one will hit
    ``LockNotAvailable`` (when ``lock_timeout`` is set) or
    ``DeadlockDetected`` (when the lock graph is cyclic). Django wraps both
    as ``django.db.utils.OperationalError`` with the original psycopg2
    exception attached on ``__cause__``.

    Detecting via the ``__cause__`` chain (rather than ``isinstance`` of the
    bare class) lets us match across psycopg2 / psycopg3 without importing
    either at module top-level — keeping this resilient if the project
    later swaps its Postgres adapter.

    Args:
        exc: The exception raised inside the webhook handler.

    Returns:
        True if *exc* (or any cause in its chain) is a recognised lock
        timeout / deadlock error, False otherwise.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        cls_name = type(current).__name__
        if cls_name in {"LockNotAvailable", "DeadlockDetected"}:
            return True
        # psycopg2 attaches ``pgcode``; SQLSTATE 55P03 = lock_not_available,
        # 40P01 = deadlock_detected. Check both as a safety net in case the
        # exception class is re-named upstream.
        pgcode = getattr(current, "pgcode", None)
        if pgcode in {"55P03", "40P01"}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _enqueue_cvv_redaction_for_donation(donation: Any) -> None:
    """Fire the CVV redaction Celery task for *donation*'s scan, if any.

    Called from the Stripe webhook handler on every authorization-related
    event (``payment_intent.payment_failed``, ``requires_action``, and
    ``succeeded``) so the CVV box is blacked out on the auth attempt
    regardless of which delivery channel reports it. The task is idempotent
    — a second fire after the synchronous CVV enqueue is a safe no-op.
    """
    placeholder = getattr(donation, "scan_placeholder", None)
    if placeholder is None:
        return

    from scans.models import ScanPlaceholder

    if placeholder.redaction_status not in (
        ScanPlaceholder.REDACTION_PENDING,
        ScanPlaceholder.REDACTION_CVV_PENDING,
    ):
        return

    try:
        from scans.tasks import apply_cvv_redaction_task

        apply_cvv_redaction_task.delay(str(placeholder.id))
    except Exception:
        logger.exception(
            "Webhook failed to enqueue apply_cvv_redaction for "
            "placeholder %s after donation %s",
            placeholder.id,
            donation.id,
        )


def _enqueue_deferred_redaction_for_donation(donation: Any) -> None:
    """Fire the post-charge redaction Celery task for *donation*'s scan, if any.

    Called from the Stripe webhook handler so a charge that's confirmed via
    ``payment_intent.succeeded`` (e.g. SCA / 3DS completion arriving after
    the sync path returned ``requires_action``) still triggers redaction.
    Idempotent: a second fire after the synchronous post-charge enqueue is
    a safe no-op. Acts as the CVV-layer safety net via
    :func:`scans.scan_redaction.apply_deferred_redaction`.
    """
    placeholder = getattr(donation, "scan_placeholder", None)
    if placeholder is None:
        return

    from scans.models import ScanPlaceholder

    if placeholder.redaction_status not in (
        ScanPlaceholder.REDACTION_PENDING,
        ScanPlaceholder.REDACTION_CVV_PENDING,
        ScanPlaceholder.REDACTION_DEFERRED,
    ):
        return

    try:
        from scans.tasks import apply_deferred_redaction_task

        apply_deferred_redaction_task.delay(str(placeholder.id))
    except Exception:
        logger.exception(
            "Webhook failed to enqueue apply_deferred_redaction for "
            "placeholder %s after donation %s",
            placeholder.id,
            donation.id,
        )


# Audit 2026-05-02 §6.1: outbound email tasks were using a fixed 30s retry
# delay. Switched to exponential backoff with jitter so a Resend / SMTP
# outage doesn't hammer the upstream once the worker pool fills with
# in-flight retries.
#
# ``autoretry_for`` is intentionally narrow: only transport-shaped
# exceptions (network / SMTP) trigger retries. Programming errors
# (``NameError`` / ``TypeError`` / ``AttributeError`` / etc.) fail
# immediately rather than burning 5 retries x backoff (~30 min of
# worker capacity) before surfacing in logs.
@shared_task(
    bind=True,
    name="core.send_batch_status_email",
    autoretry_for=(OSError, smtplib.SMTPException),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_batch_status_email(
    self: object,
    recipient_email: str,
    subject: str,
    html_message: str,
) -> dict:
    """Send batch QA status notification email asynchronously.

    Args:
        self: Celery task instance.
        recipient_email: Email address to send to.
        subject: Email subject line.
        html_message: HTML email body.

    Returns:
        dict with success status.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    try:
        logger.info(
            "Attempting to send batch status email to %s via %s:%s",
            recipient_email,
            getattr(settings, "EMAIL_HOST", "N/A"),
            getattr(settings, "EMAIL_PORT", "N/A"),
        )
        send_mail(
            subject=subject,
            message="",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info("Batch status email successfully sent to %s", recipient_email)
        return {"success": True}
    except Exception as exc:
        # Log context (recipient, EMAIL_HOST, retry counter) before
        # re-raising so both the autoretry path and the final-failure
        # path surface enough to debug. ``autoretry_for`` will then
        # decide whether to schedule another attempt based on the
        # exception type.
        retries = getattr(getattr(self, "request", None), "retries", "?")
        max_retries_val = getattr(self, "max_retries", "?")
        logger.error(
            "Failed to send batch status email to %s (Host: %s, retry %s/%s): %s",
            recipient_email,
            getattr(settings, "EMAIL_HOST", "N/A"),
            retries,
            max_retries_val,
            exc,
        )
        raise


@shared_task(
    bind=True,
    name="core.process_data_file_upload_task",
    max_retries=3,
    soft_time_limit=1500,  # 25 minutes
    time_limit=1800,  # 30 minutes
)
def process_data_file_upload_task(self: object, upload_id: str) -> dict:
    """
    Process a data file upload asynchronously

    Args:
        upload_id: UUID of the DataFileUpload instance

    Returns:
        dict: Statistics about the upload processing
    """
    from campaigns.models import DataFileUpload

    try:
        logger.info("Starting processing of upload %s", upload_id)

        # Get the upload instance
        try:
            upload = DataFileUpload.objects.get(id=upload_id)
        except DataFileUpload.DoesNotExist:
            logger.error("DataFileUpload %s not found", upload_id)
            return {"success": False, "error": "Upload not found"}

        # Check if already processing
        if upload.status == "processing":
            logger.warning("Upload %s is already being processed", upload_id)
            return {"success": False, "error": "Upload is already being processed"}

        # Process the upload
        from .utils import process_data_file_upload

        successful, failed, errors = process_data_file_upload(upload)

        logger.info(
            "Completed processing upload %s: %s successful, %s failed",
            upload_id,
            successful,
            failed,
        )

        return {
            "success": True,
            "upload_id": str(upload_id),
            "successful_imports": successful,
            "failed_imports": failed,
            "total_errors": len(errors),
            "status": upload.status,
        }

    except Exception as exc:
        logger.error("Error processing upload %s: %s", upload_id, exc, exc_info=True)

        # Update upload status
        try:
            upload = DataFileUpload.objects.get(id=upload_id)
            upload.status = "failed"
            upload.error_log = [{"error": f"Task failed: {exc!s}"}]
            upload.save()
        except Exception as e:
            logger.error("Failed to update upload status: %s", e)

        # Retry the task if within retry limit
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {"success": False, "error": str(exc)}


@shared_task(
    bind=True,
    name="core.cleanup_old_uploads",
    ignore_result=True,
    soft_time_limit=300,
    time_limit=360,
)
def cleanup_old_uploads(self: object) -> dict:
    """Periodic task to clean up old upload files and records.

    Run this as a scheduled task (e.g., daily).

    Args:
        self: Celery task instance.

    Returns:
        dict with deleted_count.
    """
    from datetime import timedelta

    from django.utils import timezone

    from campaigns.models import DataFileUpload

    cutoff_date = timezone.now() - timedelta(days=30)

    old_uploads = DataFileUpload.objects.filter(
        created_at__lt=cutoff_date, status__in=["completed", "failed"]
    )

    count = old_uploads.count()

    for upload in old_uploads:
        if upload.file:
            try:
                upload.file.delete(save=False)
            except Exception as e:
                logger.error("Error deleting file for upload %s: %s", upload.id, e)

    old_uploads.delete()

    logger.info("Cleaned up %s old upload records", count)

    return {"deleted_count": count}


@shared_task(
    bind=True,
    name="core.update_data_file_statistics",
    ignore_result=True,
    soft_time_limit=120,
    time_limit=180,
)
def update_data_file_statistics(self: object, data_file_id: str) -> dict:
    """Update statistics for a data file.

    Args:
        self: Celery task instance.
        data_file_id: Campaign ID of the CampaignDataFile.

    Returns:
        dict with success status and total_donors.
    """
    from campaigns.models import CampaignDataFile

    try:
        data_file = CampaignDataFile.objects.get(campaign_id=data_file_id)
        data_file.total_donors = data_file.donors.count()
        data_file.save(update_fields=["total_donors"])
        logger.info("Updated statistics for data file %s", data_file_id)
        return {"success": True, "total_donors": data_file.total_donors}
    except CampaignDataFile.DoesNotExist:
        logger.error("CampaignDataFile %s not found", data_file_id)
        return {"success": False, "error": "Data file not found"}
    except Exception as e:
        logger.error("Error updating statistics: %s", e)
        return {"success": False, "error": str(e)}


@shared_task(
    bind=True,
    name="core.bulk_delete_data_file_donors",
    soft_time_limit=600,
    time_limit=720,
)
def bulk_delete_data_file_donors_task(self: object, data_file_id: str) -> dict:
    """Delete all donors from a data file in the background.

    Args:
        self: Celery task instance.
        data_file_id: Campaign ID of the CampaignDataFile.

    Returns:
        dict with success status and deleted_count.
    """
    from campaigns.models import CampaignDataFile
    from donors.models import DataFileDonor

    try:
        data_file = CampaignDataFile.objects.get(campaign_id=data_file_id)
        count = data_file.donors.count()

        logger.info(
            "Starting bulk delete of %s donors from data file %s",
            count,
            data_file_id,
        )

        # Delete in batches to avoid memory issues
        batch_size = 500
        deleted = 0

        while True:
            donor_ids = list(data_file.donors.values_list("id", flat=True)[:batch_size])

            if not donor_ids:
                break

            DataFileDonor.objects.filter(id__in=donor_ids).delete()
            deleted += len(donor_ids)

            logger.info(
                "Deleted %s/%s donors from data file %s",
                deleted,
                count,
                data_file_id,
            )

        # Update statistics
        data_file.total_donors = data_file.donors.count()
        data_file.save(update_fields=["total_donors"])

        logger.info(
            "Completed bulk delete of %s donors from data file %s",
            deleted,
            data_file_id,
        )

        return {"success": True, "deleted_count": deleted}

    except CampaignDataFile.DoesNotExist:
        logger.error("CampaignDataFile %s not found", data_file_id)
        return {"success": False, "error": "Data file not found"}
    except Exception as e:
        logger.error("Error in bulk delete: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ============================================================================
# Payment Processing Tasks
# ============================================================================


def _notify_staff_of_orphan_webhook(
    event_id: str, event_type: str, error_message: str
) -> None:
    """Create a staff Notification for a webhook that exhausted its retries.

    Used when a ``charge.*`` webhook arrives, references a charge/intent that
    never gets persisted locally, and the Celery retry budget is exhausted.
    A human needs to investigate (e.g. failed ``payment_intent.succeeded``,
    Stripe account mismatch).

    Args:
        event_id: UUID of the StripeWebhookEvent.
        event_type: Stripe event type string (e.g. ``charge.refunded``).
        error_message: Human-readable error to surface in the Notification.
    """
    try:
        from core.models import User
        from notifications.models import Notification

        staff_users = User.objects.filter(is_staff=True, is_active=True)
        notifications = [
            Notification(
                user=user,
                title=f"Stripe webhook unresolved: {event_type}",
                message=(
                    f"Webhook event {event_id} could not be applied after "
                    f"max retries: {error_message}. Investigate the linked "
                    f"Stripe charge — the corresponding payment may not have "
                    f"been persisted."
                ),
                notification_type=Notification.TYPE_ERROR,
                related_object_type="StripeWebhookEvent",
                related_object_id=str(event_id),
            )
            for user in staff_users
        ]
        if notifications:
            Notification.objects.bulk_create(notifications)
    except Exception:
        logger.exception("Failed to notify staff of orphan webhook %s", event_id)


@shared_task(
    bind=True,
    name="core.process_stripe_webhook",
    max_retries=5,
    soft_time_limit=120,
    time_limit=180,
)
def process_stripe_webhook(self: Any, event_id: str) -> dict:
    """Process Stripe webhook event asynchronously.

    Retries on ``StripePaymentNotFoundError`` so charge events that arrive
    before ``payment_intent.succeeded`` finishes persisting are reprocessed
    (instead of being silently dropped). After ``max_retries``, a staff
    Notification is created and the event is left ``processed=False`` so it
    can be replayed manually.

    Args:
        event_id: UUID of StripeWebhookEvent

    Returns:
        dict: Processing result
    """
    from django.db import transaction

    from payments.campaign_payment import CampaignPaymentService
    from payments.models import StripeWebhookEvent
    from payments.services import StripePaymentNotFoundError, StripePaymentService

    _CHARGE_EVENT_HANDLERS = {
        "charge.refunded": StripePaymentService.process_charge_refunded,
        "charge.dispute.created": StripePaymentService.process_charge_dispute_created,
        "charge.dispute.closed": StripePaymentService.process_charge_dispute_closed,
    }

    event_type_for_logs = ""
    try:
        logger.info("Processing webhook event: %s", event_id)

        event = StripeWebhookEvent.objects.get(id=event_id)
        event_type_for_logs = event.event_type

        # Check if already processed
        if event.processed:
            logger.warning("Event %s already processed", event_id)
            return {"success": True, "message": "Already processed"}

        # Increment processing attempts
        event.processing_attempts += 1
        event.save(update_fields=["processing_attempts"])

        # Get event type and data
        event_type = event.event_type
        event_data = event.payload.get("data", {}).get("object", {})

        # Handle different event types
        if event_type == "payment_intent.succeeded":
            payment_intent_id = event_data.get("id")
            payment = StripePaymentService.process_successful_payment(payment_intent_id)

            if payment and payment.donation:
                # Update campaign and batch status
                donation = payment.donation
                if donation.batch:
                    CampaignPaymentService.update_campaign_payment_status(
                        donation.campaign
                    )
                # CVV first (idempotent if already done) then post-charge.
                _enqueue_cvv_redaction_for_donation(donation)
                _enqueue_deferred_redaction_for_donation(donation)

                # Phone-intake recovery: when a 3DS challenge completes
                # off-call, the donation arrives here at qa_status=pending.
                # Apply the same auto-approval rules as the synchronous
                # intake path so the donor's letter doesn't wait for
                # someone to spot it in the QA queue.
                from donations.intake import (
                    apply_phone_intake_auto_approval,
                    is_donation_auto_approve_eligible,
                )

                donation.refresh_from_db()
                if is_donation_auto_approve_eligible(donation):
                    apply_phone_intake_auto_approval(
                        donation,
                        note="Auto-approved post-3DS authentication",
                    )

        elif event_type == "payment_intent.payment_failed":
            payment_intent_id = event_data.get("id")
            error = event_data.get("last_payment_error", {})
            error_message = error.get("message", "Payment failed")
            error_code = error.get("code", "")

            payment = StripePaymentService.process_failed_payment(
                payment_intent_id, error_message, error_code
            )

            if payment and payment.donation:
                # Update campaign and batch status
                donation = payment.donation
                if donation.batch:
                    CampaignPaymentService.update_campaign_payment_status(
                        donation.campaign
                    )
                # PCI 3.2: CVV must be redacted on the auth attempt, even
                # when it fails. The PAN stays readable for retries.
                _enqueue_cvv_redaction_for_donation(donation)

        elif event_type == "payment_intent.requires_action":
            # 3DS / SCA challenge — auth has been issued, donor is being
            # asked for a step-up. CVV must already be removed; the PAN
            # stays readable so QA can dispatch a Stripe Checkout
            # authentication link if the operator-typed payment-method
            # token expired.
            from payments.models import StripePayment

            payment_intent_id = event_data.get("id") or ""
            payment = (
                StripePayment.objects.select_related("donation__scan_placeholder")
                .filter(stripe_payment_intent_id=payment_intent_id)
                .first()
            )
            if payment and payment.donation:
                _enqueue_cvv_redaction_for_donation(payment.donation)

        elif event_type in _CHARGE_EVENT_HANDLERS:
            handler = _CHARGE_EVENT_HANDLERS[event_type]
            payment = handler(event_data)
            if payment and payment.donation and payment.donation.batch:
                CampaignPaymentService.update_campaign_payment_status(
                    payment.donation.campaign
                )

        # Mark as processed — re-fetch with a row-level lock on PostgreSQL to
        # prevent two concurrent workers from both completing the same event.
        with transaction.atomic():
            from django.db import connection as db_conn
            from django.utils import timezone

            mark_qs = StripeWebhookEvent.objects
            if db_conn.vendor != "sqlite":
                mark_qs = mark_qs.select_for_update()

            refreshed = mark_qs.get(id=event_id)
            if refreshed.processed:
                logger.info(
                    "Event %s already marked processed by concurrent worker", event_id
                )
                return {"success": True, "message": "Already processed (concurrent)"}

            refreshed.processed = True
            refreshed.processed_at = timezone.now()
            refreshed.save(update_fields=["processed", "processed_at"])

        logger.info("Successfully processed webhook event: %s", event_id)
        return {"success": True, "event_type": event_type}

    except StripeWebhookEvent.DoesNotExist:
        logger.error("Webhook event %s not found", event_id)
        return {"success": False, "error": "Event not found"}

    except StripePaymentNotFoundError as exc:
        # The referenced payment hasn't been persisted yet (race with
        # payment_intent.succeeded). Persist the error and let Celery retry.
        # The event row stays processed=False so the eventual successful
        # attempt can mark it. If we exhaust retries, surface to staff.
        logger.warning(
            "Webhook %s could not locate payment yet (attempt %s/%s): %s",
            event_id,
            self.request.retries + 1,
            self.max_retries,
            exc,
        )
        try:
            event = StripeWebhookEvent.objects.get(id=event_id)
            event.processing_error = str(exc)
            event.save(update_fields=["processing_error"])
        except Exception:
            logger.exception(
                "Failed to persist Stripe webhook error for event %s", event_id
            )

        if self.request.retries < self.max_retries:
            # Exponential backoff so the originating payment_intent.succeeded
            # has time to finish persisting.
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        # Retry budget exhausted — leave processed=False, alert staff.
        _notify_staff_of_orphan_webhook(event_id, event_type_for_logs, str(exc))
        logger.error(
            "Webhook %s exhausted retries with no matching payment — staff notified",
            event_id,
        )
        return {"success": False, "error": str(exc)}

    except Exception as exc:
        # Postgres row-level lock timeout / deadlock from concurrent webhook
        # deliveries hitting the same payment (e.g. payment_intent.succeeded
        # racing charge.refunded). These are *expected transient* errors —
        # log at WARNING (not ERROR) so they don't spam Sentry, and retry
        # on a tight jittered exponential backoff so the second delivery
        # converges quickly instead of waiting the default 60+s.
        if _is_lock_timeout_or_deadlock(exc):
            lock_max_retries = 3
            if self.request.retries < lock_max_retries:
                # 0.5-2.0s base * 2^retry -> ~0.5-2s, 1-4s, 2-8s.
                countdown = random.uniform(0.5, 2.0) * (2**self.request.retries)
                logger.warning(
                    "Stripe webhook %s hit lock timeout / deadlock "
                    "(attempt %s/%s) — retrying in %.2fs: %s",
                    event_id,
                    self.request.retries + 1,
                    lock_max_retries,
                    countdown,
                    exc,
                )
                raise self.retry(
                    exc=exc, countdown=countdown, max_retries=lock_max_retries
                ) from exc

            # Retry budget exhausted — re-raise so Celery dead-letters the
            # task (the row stays processed=False so it can be replayed).
            logger.error(
                "Stripe webhook %s exhausted %s lock-retry attempts: %s",
                event_id,
                lock_max_retries,
                exc,
            )
            raise

        logger.error("Error processing webhook: %s", exc, exc_info=True)

        # Update error in event
        try:
            event = StripeWebhookEvent.objects.get(id=event_id)
            event.processing_error = str(exc)
            event.save(update_fields=["processing_error"])
        except Exception:
            logger.exception(
                "Failed to persist Stripe webhook error for event %s", event_id
            )

        # Retry with exponential backoff
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc

        return {"success": False, "error": str(exc)}


@shared_task(
    bind=True,
    name="core.send_donation_receipt",
    soft_time_limit=60,
    time_limit=90,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def send_donation_receipt(self: object, donation_id: str, payment_id: str) -> dict:
    """Send donation receipt email.

    Args:
        self: Celery task instance.
        donation_id: UUID of Donation.
        payment_id: UUID of StripePayment.

    Returns:
        dict with email send result.
    """
    from django.core.mail import EmailMessage

    from donations.models import Donation
    from payments.models import StripePayment

    try:
        logger.info("Sending receipt for donation %s", donation_id)

        donation = Donation.objects.select_related(
            "campaign", "system_donor", "donor", "data_file_donor"
        ).get(id=donation_id)
        StripePayment.objects.get(id=payment_id)

        # Get donor email
        if donation.system_donor:
            email = donation.system_donor.email
        elif donation.donor:
            email = donation.donor.email
        elif donation.data_file_donor:
            email = donation.data_file_donor.email
        else:
            logger.error("No donor found for donation %s", donation_id)
            return {"success": False, "error": "No donor"}

        from django.conf import settings
        from django.template.loader import render_to_string
        from django.utils import timezone as tz

        # Determine donor name
        if donation.system_donor:
            donor_name = donation.system_donor.full_name
        elif donation.donor:
            donor_name = donation.donor.full_name
        elif donation.data_file_donor:
            donor_name = (
                f"{donation.data_file_donor.first_name} "
                f"{donation.data_file_donor.last_name}"
            )
        else:
            donor_name = "Valued Donor"

        # Render HTML email from template
        email_context = {
            "donor_name": donor_name,
            "amount": f"{donation.amount:,.2f}",
            "currency_symbol": "£" if donation.currency == "GBP" else donation.currency,
            "campaign_name": donation.campaign.name,
            "donation_date": (
                donation.donation_date.strftime("%d/%m/%Y")
                if donation.donation_date
                else tz.now().strftime("%d/%m/%Y")
            ),
            "payment_method": donation.get_payment_method_display(),
            "reference": str(donation.id)[:8].upper(),
            "gift_aid": donation.gift_aid,
            "charity_name": (
                donation.campaign.client.name
                if hasattr(donation.campaign, "client") and donation.campaign.client
                else "The Charity"
            ),
            "year": tz.now().year,
        }

        html_body = render_to_string("email/donation_receipt.html", email_context)

        subject = f"Donation Receipt - {donation.campaign.name}"

        email_msg = EmailMessage(
            subject=subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        email_msg.content_subtype = "html"
        email_msg.send()

        logger.info("Sent receipt for donation %s to %s", donation_id, email)
        return {"success": True, "donation_id": donation_id}

    except Exception as e:
        logger.error("Failed to send donation receipt: %s", e)
        return {"success": False, "error": str(e)}


@shared_task(
    bind=True,
    name="core.retry_failed_payments",
    soft_time_limit=300,
    time_limit=360,
)
def retry_failed_payments(self: object) -> dict:
    """Periodic task to retry failed payments that are due for retry.

    Args:
        self: Celery task instance.

    Returns:
        dict with retry statistics.
    """
    from django.utils import timezone

    from payments.models import StripePayment
    from payments.services import StripePaymentService

    try:
        logger.info("Running retry_failed_payments task")

        # Get payments due for retry
        payments_to_retry = StripePayment.objects.filter(
            status=StripePayment.STATUS_FAILED,
            next_retry_at__lte=timezone.now(),
            retry_count__lt=3,
        )

        retry_count = 0
        success_count = 0
        failed_count = 0

        for payment in payments_to_retry:
            try:
                result = StripePaymentService.retry_failed_payment(str(payment.id))
                retry_count += 1

                if result.get("status") == "retry_scheduled":
                    success_count += 1
                else:
                    failed_count += 1

            except Exception as e:
                logger.error("Failed to retry payment %s: %s", payment.id, e)
                failed_count += 1

        logger.info(
            "Retry task completed: %s attempted, %s scheduled, %s failed",
            retry_count,
            success_count,
            failed_count,
        )

        return {
            "success": True,
            "attempted": retry_count,
            "scheduled": success_count,
            "failed": failed_count,
        }

    except Exception as e:
        logger.error("Error in retry_failed_payments task: %s", e)
        return {"success": False, "error": str(e)}


# ============================================================================
# Batch Approval Tasks
# ============================================================================


@shared_task(
    bind=True,
    name="core.on_batch_approved_task",
    max_retries=2,
    soft_time_limit=300,
    time_limit=360,
)
def on_batch_approved_task(self: object, batch_id: int) -> dict:
    """Orchestrate all post-approval side-effects for a DonationBatch.

    Runs after a batch is approved.  Keeps the Django signal thin — the signal
    only dispatches this task; all downstream work happens here so each step is
    independently logged and retryable.

    Steps (run in order, each wrapped so failures are isolated):
    1. Generate the HMRC Gift Aid CSV.

    Letter generation is not auto-triggered; a dedicated print operator runs
    letter batches from the letters print console.

    Args:
        self: Celery task instance.
        batch_id: Primary key of the approved DonationBatch.

    Returns:
        dict summarising which steps ran and their outcomes.
    """
    from donations.models import DonationBatch

    try:
        batch = DonationBatch.objects.select_related(
            "campaign", "campaign__client"
        ).get(id=batch_id)
    except DonationBatch.DoesNotExist:
        logger.error("on_batch_approved_task: DonationBatch %s not found", batch_id)
        return {"success": False, "error": "Batch not found"}

    results: dict[str, Any] = {"batch_id": batch_id, "steps": {}}

    try:
        gift_aid_result = _run_gift_aid_for_batch(batch)
        results["steps"]["gift_aid"] = gift_aid_result
    except Exception:
        logger.exception(
            "on_batch_approved_task: gift aid step failed for batch %s", batch_id
        )
        results["steps"]["gift_aid"] = {
            "success": False,
            "error": "unhandled exception",
        }

    results["success"] = True
    return results


def _run_gift_aid_for_batch(batch: Any) -> dict:
    """Generate the HMRC Gift Aid CSV for *batch* and persist the path.

    Args:
        batch: Approved DonationBatch instance (with campaign pre-fetched).

    Returns:
        dict with ``success``, ``row_count``, and ``filepath``.
    """
    import csv
    import os
    from decimal import Decimal

    from django.conf import settings
    from django.utils import timezone

    from donations.models import Donation

    donations = (
        Donation.objects.filter(
            batch=batch,
            gift_aid=True,
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        .select_related("donor", "data_file_donor", "campaign", "campaign__client")
        .order_by("created_at")
    )

    if not donations.exists():
        logger.info("No gift-aid eligible donations in batch %s — skipping", batch.id)
        return {"success": True, "row_count": 0, "skipped": True}

    output_dir = os.path.join(settings.MEDIA_ROOT, "gift_aid_reports")
    os.makedirs(output_dir, exist_ok=True)
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"gift_aid_batch_{batch.id}_{timestamp}.csv"
    filepath = os.path.join(output_dir, filename)
    relative_path = os.path.join("gift_aid_reports", filename)

    headers = [
        "Donation Date",
        "Donor Name",
        "URN",
        "Address",
        "Postcode",
        "Campaign",
        "Amount",
        "Gift Aid Amount",
    ]
    rows: list[list[str]] = []
    for d in donations:
        donor = d.donor or d.data_file_donor
        address_parts = (
            [
                p
                for p in [
                    getattr(donor, "address_line1", ""),
                    getattr(donor, "address_line2", ""),
                    getattr(donor, "city", ""),
                    getattr(donor, "county", ""),
                ]
                if p
            ]
            if donor
            else []
        )
        postcode = getattr(donor, "postcode", "") or "" if donor else ""
        amount: Decimal = d.amount or Decimal("0")
        gift_aid_amount = amount * Decimal("0.25")
        display_date = d.donation_date if d.donation_date else d.created_at.date()
        donor_name = ""
        if donor:
            fn = getattr(donor, "first_name", "") or ""
            ln = getattr(donor, "last_name", "") or ""
            donor_name = f"{fn} {ln}".strip()
        urn = getattr(donor, "urn", "") or "" if donor else ""
        rows.append(
            [
                display_date.strftime("%d/%m/%Y") if display_date else "-",
                donor_name or "-",
                urn or "-",
                ", ".join(address_parts) or "-",
                postcode or "-",
                d.campaign.name if d.campaign else "-",
                f"£{amount:.2f}",
                f"£{gift_aid_amount:.2f}",
            ]
        )

    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)

    # Persist to the dedicated field (clean CharField, not buried in field_data).
    batch.gift_aid_report_path = relative_path
    batch.save(update_fields=["gift_aid_report_path"])

    logger.info(
        "Gift aid report generated for batch %s: %s rows -> %s",
        batch.id,
        len(rows),
        filepath,
    )
    return {"success": True, "row_count": len(rows), "filepath": relative_path}


@shared_task(
    bind=True,
    name="core.auto_export_pending_donors_task",
    max_retries=2,
    soft_time_limit=120,
    time_limit=180,
)
def auto_export_pending_donors_task(self: object) -> dict:
    """Export pending donors into client-specific CSV files and notify staff.

    Queries every ``Donor`` with ``verification_status=pending_export``,
    writes one CSV per client to ``media/donor_exports/``, then creates an
    in-app notification for every staff user so they know the files are ready
    to send to the relevant charities.

    De-duplication: a cache key ``donor_export_lock`` is set for 6 days after
    each successful export.  If the key is still present the task returns early
    without creating a duplicate file.  This prevents the weekly beat from
    flooding the charity with the same donor list before they've had a chance to
    reconcile the previous one.

    Args:
        self: Celery task instance.

    Returns:
        dict with ``success``, ``pending_count``, ``client_count``, and
        ``filepaths`` keys.
        Returns ``skipped=True`` when there are no pending-export donors or a
        recent export already ran.
    """
    import csv
    import os
    from itertools import groupby

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.utils import timezone
    from django.utils.text import slugify

    from donors.models import Donor
    from notifications.models import Notification

    _LOCK_KEY = "donor_export_in_progress"
    _LOCK_TTL = 6 * 24 * 3600  # 6 days — won't re-export until the lock expires

    if cache.get(_LOCK_KEY):
        logger.info(
            "auto_export_pending_donors_task: recent export lock is active — skipping"
        )
        return {
            "success": True,
            "pending_count": 0,
            "skipped": True,
            "reason": "recent_export_lock",
        }

    UserModel = get_user_model()

    pending_donors = list(
        Donor.objects.filter(verification_status=Donor.VERIFICATION_PENDING_EXPORT)
        .select_related("client")
        .only(
            "id",
            "client_id",
            "first_name",
            "last_name",
            "urn",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "county",
            "postcode",
            "created_at",
            "client__id",
            "client__name",
            "client__client_code",
        )
        .order_by("client__name", "last_name", "first_name")
    )

    if not pending_donors:
        logger.info(
            "auto_export_pending_donors_task: no pending-export donors — skipping"
        )
        return {"success": True, "pending_count": 0, "skipped": True}

    output_dir = os.path.join(settings.MEDIA_ROOT, "donor_exports")
    os.makedirs(output_dir, exist_ok=True)
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")

    headers = [
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Address Line 1",
        "Address Line 2",
        "City",
        "County",
        "Postcode",
        "Created Date",
    ]

    export_files: list[dict[str, Any]] = []
    for _, donor_group in groupby(pending_donors, key=lambda donor: donor.client_id):
        donors_for_client = list(donor_group)
        first_donor = donors_for_client[0]
        client = first_donor.client
        client_name = client.name if client is not None else "Unassigned"
        client_code = (client.client_code if client is not None else "").strip()
        filename_stem = client_code or slugify(client_name) or "unassigned"
        filename = f"pending_donors_{filename_stem}_{timestamp}.csv"
        filepath = os.path.join(output_dir, filename)
        relative_path = os.path.join("donor_exports", filename)

        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for donor in donors_for_client:
                writer.writerow(
                    [
                        donor.first_name or "",
                        donor.last_name or "",
                        donor.email or "",
                        getattr(donor, "phone", "") or "",
                        getattr(donor, "address_line1", "") or "",
                        getattr(donor, "address_line2", "") or "",
                        getattr(donor, "city", "") or "",
                        getattr(donor, "county", "") or "",
                        donor.postcode or "",
                        donor.created_at.strftime("%d/%m/%Y"),
                    ]
                )

        export_files.append(
            {
                "client_name": client_name,
                "filename": filename,
                "filepath": relative_path,
                "pending_count": len(donors_for_client),
            }
        )

    # Notify all active staff users
    staff_users = UserModel.objects.filter(is_staff=True, is_active=True)
    notifications: list[Notification] = []
    exported_filenames = ", ".join(item["filename"] for item in export_files)
    for user in staff_users:
        notifications.append(
            Notification(
                user=user,
                title="Pending Donor Export Ready",
                message=(
                    f"{len(pending_donors)} donor(s) with status 'Pending Export' have been "
                    f"exported across {len(export_files)} client file(s): {exported_filenames}. "
                    f"Please send each file to the matching charity so they can assign URNs "
                    f"and return the updated house file. Import each returned house file from "
                    f"Donor Imports (House File) for the matching client."
                ),
                notification_type=Notification.TYPE_INFO,
                related_object_type="donor_export",
                link="/admin/clients/",
            )
        )
    if notifications:
        Notification.objects.bulk_create(notifications)

    # Set the de-dupe lock so the next weekly invocation skips if this file
    # hasn't been reconciled yet (lock expires after 6 days).
    cache.set(_LOCK_KEY, True, _LOCK_TTL)

    logger.info(
        "auto_export_pending_donors_task: exported %d pending donors across %d files",
        len(pending_donors),
        len(export_files),
    )
    return {
        "success": True,
        "pending_count": len(pending_donors),
        "client_count": len(export_files),
        "filepath": export_files[0]["filepath"],
        "filepaths": [item["filepath"] for item in export_files],
    }
