"""Batch payment service for processing donation batch payments via Stripe.

Handles batch-level payment processing with progress tracking and error handling.
"""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import stripe
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

if TYPE_CHECKING:
    from core.models import User
    from donations.models import Donation, DonationBatch

from payments.services import StripePaymentService

# CampaignPaymentService moved inside methods to avoid circular imports

logger = logging.getLogger(__name__)

# NOTE: stripe.api_key is set per-call, not at module level.

_CARD_PAYMENT_METHODS = {"card"}

# Status values that mean a Stripe charge is already in flight or settled for
# this donation. If a row in ``StripePayment`` for the donation carries one of
# these statuses, a second concurrent QA approval must short-circuit instead
# of creating a duplicate charge.
_ACTIVE_STRIPE_PAYMENT_STATUSES = ("pending", "processing", "succeeded")


def _supports_row_locks() -> bool:
    """Whether the active DB backend honours ``SELECT ... FOR UPDATE``.

    SQLite (used in dev/test) silently no-ops ``select_for_update`` and does
    not support the ``of=`` argument. Production (Postgres) does — gate any
    ``of=`` calls on this so the dev/test path stays portable.
    """
    return connection.vendor != "sqlite"


class BatchPaymentService:
    """Service for batch payment processing operations."""

    @staticmethod
    def process_donation_payment(
        donation: Donation,
        user: User,
        payment_method_id: str | None = None,
        require_qa_approved: bool = True,
        *,
        moto: bool = False,
        capture_immediately: bool = False,
    ) -> dict[str, Any]:
        """Process payment for one card donation.

        ``moto=True`` switches to the phone-intake flow: Stripe authorises
        the card during the call. By default settlement is deferred to QA
        approval (``capture_method="manual"`` → donation lands at
        ``requires_capture``); pass ``capture_immediately=True`` to settle
        in the same Stripe call so the donation lands at ``completed``
        directly (used for happy-path phone donations that auto-approve).
        ``require_qa_approved`` is ignored when ``moto=True`` because the
        donor is on the phone — QA happens after the auth, not before.

        Args:
            donation: Donation instance.
            user: User initiating payment.
            payment_method_id: Optional Stripe PaymentMethod ID created in browser.
            require_qa_approved: Whether donation must already be QA approved.
            moto: When ``True``, treat as a phone-intake charge (records
                MOTO metadata for compliance reporting). Has no effect on
                capture timing on its own.
            capture_immediately: When ``True``, omit ``capture_method``
                from the PaymentIntent so Stripe defaults to automatic
                capture and the charge settles synchronously. Only
                meaningful when ``moto=True``.

        Returns:
            Dict with processing outcome.
        """
        if donation.payment_method not in _CARD_PAYMENT_METHODS:
            return {"success": False, "error": "Donation is not a card payment"}

        if (
            not moto
            and require_qa_approved
            and donation.qa_status != donation.QA_STATUS_APPROVED
        ):
            return {"success": False, "error": "Donation is not QA approved"}

        if donation.payment_status in {
            "processing",
            "completed",
            "requires_capture",
            "awaiting_authentication",
        }:
            return {
                "success": True,
                "skipped": True,
                "message": (
                    "Donation payment already processing, completed, "
                    "awaiting capture, or awaiting donor authentication"
                ),
            }

        has_active_payment = donation.stripe_payments.filter(
            status__in=["pending", "processing", "requires_capture", "succeeded"]
        ).exists()
        if has_active_payment:
            return {
                "success": True,
                "skipped": True,
                "message": "Donation already has an active Stripe payment",
            }

        result = BatchPaymentService._process_single_donation(
            donation,
            user,
            payment_method_id=payment_method_id,
            moto=moto,
            capture_immediately=capture_immediately,
        )

        from payments.campaign_payment import CampaignPaymentService

        CampaignPaymentService.update_campaign_payment_status(donation.campaign)
        return result

    @staticmethod
    def get_credit_card_donations(batch: DonationBatch):
        """Get all credit card donations in batch that need processing.

        Args:
            batch: DonationBatch instance

        Returns:
            QuerySet of donations with payment_method='card'
        """
        return batch.donations.filter(
            payment_method__in=_CARD_PAYMENT_METHODS,
            payment_status__in=["pending", "failed"],
        ).select_related("donor", "data_file_donor", "campaign")

    @staticmethod
    def get_batch_payment_summary(batch: DonationBatch) -> dict[str, Any]:
        """Get payment summary for batch.

        Args:
            batch: DonationBatch instance

        Returns:
            Dict with batch payment statistics
        """
        credit_card_donations = batch.donations.filter(payment_method="card")

        total = credit_card_donations.count()
        total_amount_agg = credit_card_donations.aggregate(total=Sum("amount"))
        total_amount = total_amount_agg["total"] or 0

        pending = credit_card_donations.filter(payment_status="pending").count()
        processing = credit_card_donations.filter(payment_status="processing").count()
        completed = credit_card_donations.filter(payment_status="completed").count()
        failed = credit_card_donations.filter(payment_status="failed").count()

        return {
            "batch_id": batch.id,
            "batch_name": batch.batch_name,
            "total_donations": total,
            "total_amount": float(total_amount),
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "payment_status": batch.payment_status,
        }

    @staticmethod
    def process_batch_payments(
        batch: DonationBatch,
        user: User,
        task_progress_callback: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        """Process all credit card donations in batch via Stripe.

        Args:
            batch: DonationBatch instance
            user: User initiating payment
            task_progress_callback: Optional callback for progress updates

        Returns:
            Dict with processing results
        """
        logger.info("Starting batch payment processing for batch %s", batch.id)

        # Get donations to process
        donations = BatchPaymentService.get_credit_card_donations(batch)
        total_count = donations.count()

        if total_count == 0:
            return {
                "success": True,
                "message": "No donations to process",
                "total": 0,
                "successful": 0,
                "failed": 0,
            }

        # Update batch status
        with transaction.atomic():
            batch.payment_status = "processing"
            batch.payment_initiated_at = timezone.now()
            batch.payment_initiated_by = user
            batch.save()

            # Update campaign status to 'in_progress'
            from payments.campaign_payment import CampaignPaymentService

            CampaignPaymentService.update_campaign_payment_status(batch.campaign)

        # Process each donation
        successful = 0
        failed = 0
        errors = []
        processed = 0

        for donation in donations:
            try:
                processed += 1

                # Progress callback
                if task_progress_callback:
                    task_progress_callback(processed, total_count, successful, failed)

                # Process payment
                result = BatchPaymentService._process_single_donation(donation, user)

                if result["success"]:
                    successful += 1
                else:
                    failed += 1
                    errors.append(
                        {
                            "donation_id": str(donation.id),
                            "error": result.get("error", "Unknown error"),
                        }
                    )

            except Exception as e:
                logger.error("Error processing donation %s: %s", donation.id, e)
                failed += 1
                errors.append({"donation_id": str(donation.id), "error": str(e)})

            # Give SQLite a chance to release locks in local development environments
            time.sleep(0.05)

        # Update batch final status
        with transaction.atomic():
            batch.successful_payment_count = successful
            batch.failed_payment_count = failed
            batch.payment_completed_at = timezone.now()

            if failed == 0:
                batch.payment_status = "completed"
            elif successful == 0:
                batch.payment_status = "failed"
            else:
                batch.payment_status = "partially_completed"

            batch.save()

            # Update campaign status (determine if overall status changed)
            from payments.campaign_payment import CampaignPaymentService

            CampaignPaymentService.update_campaign_payment_status(batch.campaign)

        logger.info(
            f"Batch {batch.id} processing completed: {successful} successful, {failed} failed"
        )

        return {
            "success": True,
            "batch_id": batch.id,
            "total": total_count,
            "successful": successful,
            "failed": failed,
            "errors": errors,
        }

    @staticmethod
    def _process_single_donation(
        donation: Donation,
        user: User,
        payment_method_id: str | None = None,
        *,
        moto: bool = False,
        capture_immediately: bool = False,
    ) -> dict[str, Any]:
        """Process a single donation payment via Stripe.

        Records every Stripe attempt in ``StripePaymentAttempt`` on a separate
        autocommit connection so the audit trail survives an outer
        ``transaction.set_rollback(True)`` triggered by the QA approval flow.

        Concurrency guard: the ``payment_status`` flip from ``pending`` →
        ``processing`` runs under a ``SELECT ... FOR UPDATE`` row lock on the
        ``Donation`` row, with a second ``stripe_payments`` re-check inside
        the lock. This closes the TOCTOU window where two concurrent QA
        approvers (or a QA approver + a batch payment retry) both pass the
        ``has_active_payment`` check in
        :meth:`process_donation_payment` and race to create two
        ``StripePayment`` rows for the same donation.

        Args:
            donation: Donation instance
            user: User processing payment
            payment_method_id: Optional Stripe PaymentMethod ID created in browser
            moto: When True, tag the PaymentIntent with phone-intake
                metadata for compliance reporting.
            capture_immediately: When True (and ``moto=True``), omit
                ``capture_method`` so Stripe defaults to automatic capture
                — the charge settles synchronously and the donation lands
                at ``completed``. When False with ``moto=True``, sets
                ``capture_method="manual"`` so the donation lands at
                ``requires_capture`` and settlement is deferred to QA.

        Returns:
            Dict with processing result
        """
        from donations.models import Donation as DonationModel
        from payments.models import StripePayment, StripePaymentAttempt

        amount_cents = int(donation.amount * 100)
        currency_code = donation.currency
        attempt_number = StripePaymentAttempt.objects.next_attempt_number(donation)

        try:
            # Acquire a row-level lock on the Donation and re-check for an
            # active Stripe payment under the lock. On Postgres this serialises
            # two concurrent approvers; on SQLite ``select_for_update`` is a
            # silent no-op but the re-check still defeats the in-process
            # interleave the test suite simulates.
            with transaction.atomic():
                if _supports_row_locks():
                    lock_qs = DonationModel.objects.select_for_update(of=("self",))
                else:
                    lock_qs = DonationModel.objects.select_for_update()
                locked_donation = lock_qs.get(pk=donation.pk)

                if locked_donation.payment_status in {
                    "processing",
                    "completed",
                    "requires_capture",
                    "awaiting_authentication",
                }:
                    return {
                        "success": True,
                        "skipped": True,
                        "message": (
                            "Donation payment already processing, completed, "
                            "awaiting capture, or awaiting donor authentication"
                        ),
                    }

                if locked_donation.stripe_payments.filter(
                    status__in=_ACTIVE_STRIPE_PAYMENT_STATUSES
                ).exists():
                    return {
                        "success": True,
                        "skipped": True,
                        "message": "Donation already has an active Stripe payment",
                    }

                locked_donation.payment_status = "processing"
                locked_donation.save(update_fields=["payment_status"])

            # Sync the caller's in-memory copy with the now-committed flip so
            # downstream donor lookups read the latest values.
            donation.refresh_from_db()

            if donation.system_donor:
                email = donation.system_donor.email
                name = (
                    f"{donation.system_donor.first_name} "
                    f"{donation.system_donor.last_name}"
                )
                donor = None
            elif donation.donor:
                email = donation.donor.email
                name = f"{donation.donor.first_name} {donation.donor.last_name}"
                donor = donation.donor
            elif donation.data_file_donor:
                email = donation.data_file_donor.email
                name = f"{donation.data_file_donor.first_name} {donation.data_file_donor.last_name}"
                donor = None
            else:
                raise ValueError("Donation has no associated donor")

            api_key = StripePaymentService._get_api_key(donation.campaign.client)

            customer_metadata: dict[str, Any] = {
                "donation_id": str(donation.id),
                "campaign_id": str(donation.campaign.id),
            }
            if moto:
                customer_metadata["moto_intake"] = "true"
                customer_metadata["intake_channel"] = "phone"
                customer_metadata["consent_recorded_at"] = timezone.now().isoformat()

            customer = StripePaymentService.get_or_create_customer(
                email=email,
                name=name,
                client=donation.campaign.client,
                donor=donor,
                metadata=customer_metadata,
            )

            currency = currency_code.lower()

            if payment_method_id:
                payment_method = stripe.PaymentMethod.retrieve(
                    payment_method_id,
                    api_key=api_key,
                )
                card_details = getattr(payment_method, "card", None)
                update_fields: list[str] = []

                last4 = getattr(card_details, "last4", "") if card_details else ""
                if last4 and donation.card_last_four != last4:
                    donation.card_last_four = last4
                    update_fields.append("card_last_four")

                exp_month = getattr(card_details, "exp_month", None)
                exp_year = getattr(card_details, "exp_year", None)
                expiry = (
                    f"{int(exp_month):02d}/{exp_year}" if exp_month and exp_year else ""
                )
                if expiry and donation.card_expiry_date != expiry:
                    donation.card_expiry_date = expiry
                    update_fields.append("card_expiry_date")

                billing_details = getattr(payment_method, "billing_details", None)
                billing_name = (
                    getattr(billing_details, "name", "") if billing_details else ""
                )
                if billing_name and donation.card_holder_name != billing_name:
                    donation.card_holder_name = billing_name
                    update_fields.append("card_holder_name")

                if update_fields:
                    donation.save(update_fields=update_fields)

            # Idempotency key derived from donation + attempt counter so a
            # network-level retry of the SAME attempt dedupes server-side
            # (Stripe replays the original response instead of charging the
            # card a second time). A genuine *new* attempt — recorded as a
            # fresh row in StripePaymentAttempt — gets a different
            # attempt_number and therefore a fresh key, which Stripe treats
            # as a separate charge.
            idempotency_key = f"don_{donation.id}_attempt_{attempt_number}"

            payment_intent_params: dict[str, Any] = {
                "amount": amount_cents,
                "currency": currency,
                "description": f"Donation to {donation.campaign.name}",
                "metadata": {
                    "donation_id": str(donation.id),
                    "campaign_id": str(donation.campaign.id),
                    "batch_id": str(donation.batch.id),
                    "intake_channel": "phone" if moto else "scan",
                    "moto": "true" if moto else "false",
                },
                "confirm": True,
                "api_key": api_key,
                "idempotency_key": idempotency_key,
            }

            # Phone intake (MOTO):
            # * ``capture_immediately=True`` (happy-path auto-approve): omit
            #   ``capture_method`` so Stripe defaults to automatic capture —
            #   funds settle in one round-trip and the donation lands at
            #   ``completed`` directly.
            # * ``capture_immediately=False`` (flagged donor / DD-in-future):
            #   set ``capture_method="manual"`` so the intent lands at
            #   ``requires_capture`` after confirm. Funds are held by the
            #   issuer until ``PaymentIntent.capture`` is called (typically
            #   on QA approval) or auto-released after 7 days.
            #
            # Note: the MOTO SCA exemption flag
            # (``payment_method_options.card.moto=true``) is NOT set here.
            # Stripe rejects that parameter as "unknown" unless the merchant
            # account has MOTO capability enabled (a Stripe-side request).
            # Without the exemption, some cards will require 3DS — the
            # existing ``send_authentication_link_for_donation`` recovery
            # flow emails the donor a Stripe Checkout link to complete SCA.
            # Enable the exemption later by (a) requesting MOTO from
            # Stripe support, then (b) re-adding the parameter here behind
            # a per-client capability flag.
            if moto and not capture_immediately:
                payment_intent_params["capture_method"] = "manual"

            if email:
                payment_intent_params["receipt_email"] = email

            if payment_method_id:
                payment_intent_params["payment_method"] = payment_method_id
                # Match the no-PM branch: dashboard-enabled redirect methods (e.g. Klarna)
                # require return_url; QA batch flow is card-only, no off-site redirects.
                payment_intent_params["automatic_payment_methods"] = {
                    "enabled": True,
                    "allow_redirects": "never",
                }
            else:
                payment_intent_params["customer"] = customer.stripe_customer_id
                payment_intent_params["automatic_payment_methods"] = {
                    "enabled": True,
                    "allow_redirects": "never",
                }

            payment_intent = stripe.PaymentIntent.create(**payment_intent_params)

            intent_id = getattr(payment_intent, "id", "") or ""
            intent_status = getattr(payment_intent, "status", "") or ""

            # PCI DSS Requirement 3.2: CVV/CVC must not be stored after the
            # authorization attempt. The auth has just happened — fire the
            # CVV redaction task now, regardless of intent_status. The task
            # is idempotent and a no-op when the CVV pass has already run.
            BatchPaymentService._enqueue_cvv_redaction(donation)

            if intent_status == "succeeded":
                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency_code,
                    status=StripePaymentAttempt.STATUS_SUCCEEDED,
                    attempt_number=attempt_number,
                    stripe_payment_intent_id=intent_id,
                )

                payment = StripePayment.objects.create(
                    stripe_payment_intent_id=intent_id,
                    stripe_customer=customer,
                    donation=donation,
                    amount=donation.amount,
                    currency=donation.currency,
                    status=StripePayment.STATUS_SUCCEEDED,
                    description=f"Donation to {donation.campaign.name}",
                    metadata={
                        "donation_id": str(donation.id),
                        "campaign_id": str(donation.campaign.id),
                    },
                    processed_by=user,
                    stripe_response=StripePaymentService._persist_intent_snapshot(
                        payment_intent
                    ),
                    stripe_charge_id=(
                        payment_intent.latest_charge
                        if payment_intent.latest_charge
                        else ""
                    ),
                )

                donation.payment_status = "completed"
                donation.save(update_fields=["payment_status"])

                BatchPaymentService._enqueue_deferred_redaction(donation)

                logger.info("Successfully processed donation %s", donation.id)
                return {"success": True, "payment_id": str(payment.id)}

            if intent_status == "requires_capture":
                # Phone-intake MOTO auth-only path: card is authorised, funds
                # are held by the issuer, but settlement waits for QA approval
                # (``StripePaymentService.capture_payment_intent``). Don't
                # enqueue deferred redaction yet — it fires when the capture
                # succeeds. The CVV redaction was already enqueued above.
                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency_code,
                    status=StripePaymentAttempt.STATUS_SUCCEEDED,
                    attempt_number=attempt_number,
                    stripe_payment_intent_id=intent_id,
                )

                payment = StripePayment.objects.create(
                    stripe_payment_intent_id=intent_id,
                    stripe_customer=customer,
                    donation=donation,
                    amount=donation.amount,
                    currency=donation.currency,
                    status=StripePayment.STATUS_REQUIRES_CAPTURE,
                    description=f"Donation to {donation.campaign.name}",
                    metadata={
                        "donation_id": str(donation.id),
                        "campaign_id": str(donation.campaign.id),
                        "moto": "true",
                    },
                    processed_by=user,
                    stripe_response=StripePaymentService._persist_intent_snapshot(
                        payment_intent
                    ),
                )

                donation.payment_status = donation.PAYMENT_STATUS_REQUIRES_CAPTURE
                donation.save(update_fields=["payment_status"])

                logger.info(
                    "MOTO authorisation succeeded for donation %s — payment %s "
                    "awaiting capture at QA approval",
                    donation.id,
                    payment.id,
                )
                return {
                    "success": True,
                    "requires_capture": True,
                    "payment_id": str(payment.id),
                    "message": (
                        "Card authorised. Funds will settle when QA approves "
                        "the donation."
                    ),
                }

            if intent_status == "requires_payment_method":
                error = getattr(payment_intent, "last_payment_error", None)
                raw_error_message = getattr(error, "message", None)
                error_message = (
                    raw_error_message
                    if isinstance(raw_error_message, str) and raw_error_message
                    else "Payment method declined"
                )
                error_code = getattr(error, "code", "") or ""
                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency_code,
                    status=StripePaymentAttempt.STATUS_FAILED,
                    attempt_number=attempt_number,
                    stripe_payment_intent_id=intent_id,
                    error_code=str(error_code),
                    error_message=error_message,
                )

                donation.payment_status = "failed"
                donation.save(update_fields=["payment_status"])

                return {"success": False, "error": error_message}

            if intent_status == "requires_action":
                # 3DS / SCA challenge required. Don't fail — persist the
                # PaymentIntent + recovery handles so QA can dispatch a
                # Stripe Checkout authentication link to the donor. The
                # webhook handler for ``payment_intent.succeeded`` will flip
                # the donation to ``completed`` once the donor authenticates.
                next_action = getattr(payment_intent, "next_action", None)
                use_stripe_sdk = (
                    getattr(next_action, "use_stripe_sdk", None)
                    if next_action is not None
                    else None
                )
                next_action_url = ""
                if use_stripe_sdk is not None:
                    raw_url = getattr(use_stripe_sdk, "stripe_js", "") or ""
                    next_action_url = str(raw_url)

                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency_code,
                    status=StripePaymentAttempt.STATUS_REQUIRES_ACTION,
                    attempt_number=attempt_number,
                    stripe_payment_intent_id=intent_id,
                    error_message="3DS/SCA authentication required",
                )

                payment = StripePayment.objects.create(
                    stripe_payment_intent_id=intent_id,
                    stripe_customer=customer,
                    donation=donation,
                    amount=donation.amount,
                    currency=donation.currency,
                    status=StripePayment.STATUS_PENDING,
                    description=f"Donation to {donation.campaign.name}",
                    metadata={
                        "donation_id": str(donation.id),
                        "campaign_id": str(donation.campaign.id),
                    },
                    processed_by=user,
                    stripe_response=StripePaymentService._persist_intent_snapshot(
                        payment_intent
                    ),
                    requires_action_url=next_action_url,
                )

                donation.payment_status = (
                    donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION
                )
                donation.save(update_fields=["payment_status"])

                logger.info(
                    "Donation %s requires 3DS/SCA — payment %s queued for "
                    "authentication link",
                    donation.id,
                    payment.id,
                )
                return {
                    "success": True,
                    "awaiting_authentication": True,
                    "payment_id": str(payment.id),
                    "message": (
                        "3DS / SCA authentication required. Send the donor an "
                        "authentication link to complete this payment."
                    ),
                }

            error_message = (
                "Payment requires additional customer action and cannot be "
                f"approved from QA: {intent_status}"
            )
            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=amount_cents,
                currency=currency_code,
                status=StripePaymentAttempt.STATUS_REQUIRES_ACTION,
                attempt_number=attempt_number,
                stripe_payment_intent_id=intent_id,
                error_message=error_message,
            )

            donation.payment_status = "failed"
            donation.save(update_fields=["payment_status"])

            return {
                "success": False,
                "error": error_message,
            }

        except stripe.StripeError as e:
            logger.error("Stripe error for donation %s: %s", donation.id, e)

            error_code = getattr(e, "code", "") or ""
            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=amount_cents,
                currency=currency_code,
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=attempt_number,
                error_code=str(error_code),
                error_message=str(e),
            )

            donation.payment_status = "failed"
            donation.save(update_fields=["payment_status"])

            return {"success": False, "error": str(e)}

        except Exception as e:
            logger.error("Error processing donation %s: %s", donation.id, e)

            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=amount_cents,
                currency=currency_code,
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=attempt_number,
                error_message=str(e),
            )

            donation.payment_status = "failed"
            donation.save(update_fields=["payment_status"])

            return {"success": False, "error": str(e)}

    @staticmethod
    def _enqueue_cvv_redaction(donation: Donation) -> None:
        """Fire the CVV-only redaction Celery task for *donation*'s scan, if any.

        Called immediately after every Stripe ``PaymentIntent.create()``
        return — success, requires_action, requires_payment_method, or any
        failure status. PCI DSS Requirement 3.2 forbids storing sensitive
        authentication data after authorization, so the CVV box must be
        blacked out at the moment of authorization, not later.

        Idempotent at the task level: a no-op when the CVV pass has already
        run, when no placeholder exists (manually entered card), or when
        no CVV coords were saved.
        """
        placeholder = getattr(donation, "scan_placeholder", None)
        if placeholder is None:
            return

        from scans.models import ScanPlaceholder

        # Status gate is also enforced inside the task; the local check is
        # an optimisation to avoid a wasted enqueue when we already know
        # the CVV pass has run.
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
                "Failed to enqueue apply_cvv_redaction for placeholder %s "
                "after donation %s; CVV redaction will be retried via the "
                "Stripe webhook handler or the retention TTL sweeper",
                placeholder.id,
                donation.id,
            )

    @staticmethod
    def _enqueue_deferred_redaction(donation: Donation) -> None:
        """Fire the post-charge redaction Celery task for *donation*'s scan, if any.

        Called immediately after a synchronous Stripe charge succeeds. The
        task is idempotent — duplicate fires from the webhook handler or
        an operator-marks-failed action are no-ops. Acts as a safety net
        for the CVV layer too: if the CVV task didn't run, the post-charge
        task applies CVV first inside the same row lock.
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
                "Failed to enqueue apply_deferred_redaction for placeholder %s "
                "after donation %s; redaction will be retried via the Stripe "
                "webhook handler or the retention TTL sweeper",
                placeholder.id,
                donation.id,
            )

    @staticmethod
    def retry_failed_donations(batch: DonationBatch, user: User) -> dict[str, Any]:
        """Retry all failed payments in batch.

        Args:
            batch: DonationBatch instance
            user: User initiating retry

        Returns:
            Dict with retry results
        """
        logger.info("Retrying failed payments for batch %s", batch.id)

        # Get failed donations
        failed_donations = batch.donations.filter(
            payment_method="card", payment_status="failed"
        )

        if not failed_donations.exists():
            return {
                "success": True,
                "message": "No failed donations to retry",
                "retried": 0,
            }

        # Reset status to pending for retry
        failed_donations.update(payment_status="pending")

        # Process batch again
        result = BatchPaymentService.process_batch_payments(batch, user)

        return result
