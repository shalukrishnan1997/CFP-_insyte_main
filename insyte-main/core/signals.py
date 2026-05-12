"""Signals for core app.

Handles automatic cleanup and data management.
"""

import logging
from typing import Any

from django.db import transaction
from django.db.models import Model
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_save, sender="campaigns.Campaign")
def handle_campaign_status_change(
    sender: type[Model], instance: Model, **kwargs: Any
) -> None:
    """
    Handle campaign status changes
    When a campaign is closed, delete associated data file donors
    """
    if instance.pk:  # Only for existing campaigns
        # Use instance-based access or string constants to avoid class imports
        Campaign_STATUS_CLOSED = "closed"

        from campaigns.models import CampaignDataFile

        try:
            old_campaign = sender.objects.filter(pk=instance.pk).only("status").first()
            if old_campaign is None:
                return

            # Check if status changed to closed
            if (
                old_campaign.status != Campaign_STATUS_CLOSED
                and instance.status == Campaign_STATUS_CLOSED
            ):
                # Delete data file and all associated donors
                try:
                    data_file = CampaignDataFile.objects.get(campaign=instance)
                    # This will cascade delete all DataFileDonor entries
                    data_file.delete()
                    logger.info(
                        "Deleted data file for closed campaign: %s", instance.name
                    )
                except CampaignDataFile.DoesNotExist:
                    # No data file exists, nothing to delete
                    pass
        except Exception as e:
            logger.exception("Error in handle_campaign_status_change: %s", e)


@receiver(post_save, sender="donors.DataFileDonor")
def update_data_file_donor_count(
    sender: type[Model], instance: Model, created: bool, **kwargs: Any
) -> None:
    """
    Update donor count in CampaignDataFile when donors are added/removed
    """
    if created:
        data_file = instance.data_file
        data_file.total_donors = data_file.donors.count()
        data_file.save(update_fields=["total_donors"])


@receiver(pre_save, sender="donations.DonationBatch")
def capture_batch_old_status(
    sender: type[Model], instance: Model, **kwargs: Any
) -> None:
    """
    Capture the old status of the batch before saving to detect changes in post_save.
    """
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._old_status = old_instance.status
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender="donations.DonationBatch")
def notify_batch_status_change(
    sender: type[Model],
    instance: Model,
    created: bool,
    update_fields: frozenset[str] | None,
    **kwargs: Any,
) -> None:
    """Send email and in-app notifications when batch QA status changes.

    Notifies the batch creator about status updates (approved/rejected/re-check)
    via both email and in-app notifications.
    """
    if created:
        return

    # Determine if status changed
    status_changed = False
    if (update_fields is not None and "status" in update_fields) or (
        hasattr(instance, "_old_status") and instance._old_status != instance.status
    ):
        status_changed = True

    if not status_changed:
        return

    # Skip if there's no creator
    if not instance.created_by:
        return

    # Prepare messages based on status
    status_config = {
        instance.STATUS_APPROVED: {
            "email_subject": f"✓ Batch Approved: {instance.batch_name}",
            "notif_title": "Batch Approved",
            "notif_type": "success",
            "email_message": f"""
                <h2 style="color: #059669;">✓ Your donation batch has been approved</h2>
                <p><strong>Batch:</strong> {instance.batch_name}</p>
                <p><strong>Client:</strong> {instance.campaign.client.name if instance.campaign and instance.campaign.client else "N/A"}</p>
                <p><strong>Campaign:</strong> {instance.campaign.name if instance.campaign else "N/A"}</p>
                <p><strong>Reviewed by:</strong> {instance.reviewed_by.get_full_name() if instance.reviewed_by else "N/A"}</p>
                <p><strong>Review notes:</strong> {instance.review_notes or "No notes provided"}</p>
                <p style="background-color: #d1fae5; padding: 10px; border-left: 3px solid #059669; margin-top: 15px;">
                    The donations in this batch are now ready for processing.
                </p>
            """,
            "notif_message": f"Your batch '{instance.batch_name}' has been approved by {instance.reviewed_by.get_full_name() if instance.reviewed_by else 'QA team'}.",
        },
        instance.STATUS_REJECTED: {
            "email_subject": f"✗ Batch Rejected - Action Required: {instance.batch_name}",
            "notif_title": "Batch Rejected - Action Required",
            "notif_type": "error",
            "email_message": f"""
                <h2 style="color: #dc2626;">✗ Your donation batch requires corrections</h2>
                <p><strong>Batch:</strong> {instance.batch_name}</p>
                <p><strong>Client:</strong> {instance.campaign.client.name if instance.campaign and instance.campaign.client else "N/A"}</p>
                <p><strong>Campaign:</strong> {instance.campaign.name if instance.campaign else "N/A"}</p>
                <p><strong>Reviewed by:</strong> {instance.reviewed_by.get_full_name() if instance.reviewed_by else "N/A"}</p>
                <p><strong>Reason for rejection:</strong></p>
                <p style="background-color: #fee2e2; padding: 10px; border-left: 3px solid #dc2626;">
                    {instance.review_notes or "No specific reason provided"}
                </p>
                <p style="margin-top: 15px;">
                    <strong>Next Steps:</strong><br>
                    1. Review the feedback above<br>
                    2. Make necessary corrections to the donations<br>
                    3. Update the batch status to resubmit for QA review
                </p>
            """,
            "notif_message": f"Your batch '{instance.batch_name}' was rejected. Reason: {instance.review_notes or 'See batch details'}. Please make corrections and resubmit.",
        },
        instance.STATUS_PENDING_QA: {
            "email_subject": f"⚠ Batch Re-check Required: {instance.batch_name}",
            "notif_title": "Batch Re-check Required",
            "notif_type": "warning",
            "email_message": f"""
                <h2 style="color: #d97706;">⚠ Please review and resubmit your donation batch</h2>
                <p><strong>Batch:</strong> {instance.batch_name}</p>
                <p><strong>Client:</strong> {instance.campaign.client.name if instance.campaign and instance.campaign.client else "N/A"}</p>
                <p><strong>Campaign:</strong> {instance.campaign.name if instance.campaign else "N/A"}</p>
                <p><strong>Reviewed by:</strong> {instance.reviewed_by.get_full_name() if instance.reviewed_by else "N/A"}</p>
                <p><strong>Review notes:</strong></p>
                <p style="background-color: #fef3c7; padding: 10px; border-left: 3px solid #d97706;">
                    {instance.review_notes or "No specific notes provided"}
                </p>
                <p style="margin-top: 15px;">
                    Please review the batch and resubmit it for QA approval.
                </p>
            """,
            "notif_message": f"Your batch '{instance.batch_name}' requires re-checking. Notes: {instance.review_notes or 'See batch details'}",
        },
    }

    # Get configuration for current status
    config = status_config.get(instance.status)
    if not config:
        return  # No notification for this status

    # Create in-app notification
    from notifications.models import Notification

    # Build link safely with null guards
    batch_link = ""
    if instance.campaign and instance.campaign.client:
        batch_link = f"/admin/donations/{instance.campaign.client.id}/{instance.campaign.id}/batch/{instance.id}/edit/"

    try:
        Notification.objects.create(
            user=instance.created_by,
            title=config["notif_title"],
            message=config["notif_message"],
            notification_type=config["notif_type"],
            related_object_type="DonationBatch",
            related_object_id=str(instance.id),
            link=batch_link,
        )
    except Exception as e:
        logger.exception("Failed to create in-app notification: %s", e)

    # Send email asynchronously via Celery (don't block the HTTP request).
    # Scheduled via transaction.on_commit so the email is only dispatched if
    # the surrounding DB transaction commits successfully — a rollback must
    # not leak a status email to the batch creator.
    if instance.created_by.email:
        from core.tasks import send_batch_status_email

        recipient_email = instance.created_by.email
        email_subject = config["email_subject"]
        email_message = config["email_message"]
        transaction.on_commit(
            lambda: send_batch_status_email.delay(
                recipient_email=recipient_email,
                subject=email_subject,
                html_message=email_message,
            )
        )

    # On approval: delegate all downstream side-effects (letters, gift aid CSV)
    # to a dedicated Celery task so the signal stays thin and each step is
    # independently logged and retryable. Also gated on commit so a rollback
    # never triggers letter generation or gift-aid CSV writes.
    if instance.status == instance.STATUS_APPROVED:
        try:
            from core.tasks import on_batch_approved_task

            batch_pk = instance.id
            transaction.on_commit(
                lambda: on_batch_approved_task.delay(batch_pk),
            )
        except Exception:
            logger.exception(
                "Failed to queue on_batch_approved_task for batch %s", instance.id
            )


@receiver(post_save, sender="donations.DonationBatch")
def update_campaign_on_batch_save(
    sender: type[Model], instance: Model, **kwargs: Any
) -> None:
    """Update campaign payment status when a batch is saved."""
    from payments.campaign_payment import CampaignPaymentService

    CampaignPaymentService.update_campaign_payment_status(instance.campaign)


@receiver(post_delete, sender="donations.DonationBatch")
def update_campaign_on_batch_delete(
    sender: type[Model], instance: Model, **kwargs: Any
) -> None:
    """Update campaign payment status when a batch is deleted."""
    # The batch's campaign might still be accessible
    try:
        from payments.campaign_payment import CampaignPaymentService

        CampaignPaymentService.update_campaign_payment_status(instance.campaign)
    except Exception:
        logger.exception("Failed to update campaign payment status on batch delete")
