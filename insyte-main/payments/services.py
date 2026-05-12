"""Stripe payment service for handling all payment operations.

Provides centralized payment processing, customer management, and error handling
for Stripe payment intents, checkout sessions, refunds, and retries.
"""

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import stripe
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.metrics import record_payment_attempt

if TYPE_CHECKING:
    from core.models import (
        Client,
        Invoice,
        User,
    )
    from donations.models import Donation
    from donors.models import Donor
    from payments.models import PaymentGatewayConfig, StripeCustomer, StripePayment

logger = logging.getLogger(__name__)

# NOTE: stripe.api_key is set per-call via _get_api_key(), not at module level.


def _client_label(client: Client | None) -> str | None:
    """Best-effort client name for Prometheus labels (avoids high-cardinality IDs)."""
    if client is None:
        return None
    return getattr(client, "name", None)


# Stripe key prefix allow-lists. Pasting a key into the wrong slot (e.g. a
# secret key into the publishable slot) silently breaks Stripe.js, so both
# the Django admin and the bespoke client-payment-config view validate
# against these prefixes before persisting.
STRIPE_PUBLISHABLE_KEY_PREFIXES = ("pk_test_", "pk_live_")
STRIPE_SECRET_KEY_PREFIXES = ("sk_test_", "sk_live_", "rk_test_", "rk_live_")
STRIPE_WEBHOOK_SECRET_PREFIX = "whsec_"


def validate_stripe_key_prefixes(
    *,
    publishable_key: str = "",
    secret_key: str = "",
    webhook_secret: str = "",
) -> str | None:
    """Validate Stripe key prefixes; return an error message or ``None``.

    Empty values are allowed (a caller may want to clear a credential). The
    returned message is suitable for display to the operator.
    """
    if publishable_key and not publishable_key.startswith(
        STRIPE_PUBLISHABLE_KEY_PREFIXES
    ):
        return (
            "Stripe publishable key must start with 'pk_test_' or 'pk_live_'. "
            "Copy the Publishable key from Stripe Dashboard → Developers → API keys."
        )
    if secret_key and not secret_key.startswith(STRIPE_SECRET_KEY_PREFIXES):
        return (
            "Stripe secret key must start with 'sk_test_'/'sk_live_' "
            "(or 'rk_*' for restricted keys)."
        )
    if webhook_secret and not webhook_secret.startswith(STRIPE_WEBHOOK_SECRET_PREFIX):
        return "Stripe webhook signing secret must start with 'whsec_'."
    return None


class StripePaymentError(Exception):
    """Custom exception for Stripe payment errors."""


class StripePaymentNotFoundError(Exception):
    """Raised when a Stripe webhook references a charge that has no local payment.

    Typically this happens when ``charge.refunded`` or ``charge.dispute.created``
    arrives before ``payment_intent.succeeded`` has finished persisting the
    ``stripe_charge_id`` on the local ``StripePayment``. The webhook task should
    convert this exception into a Celery retry so the event is reprocessed once
    the payment row is up to date.
    """


class StripePaymentService:
    """Centralized service for Stripe payment operations."""

    @staticmethod
    def _get_active_stripe_config(client: Client | None) -> PaymentGatewayConfig:
        """Return the active Stripe config row for ``client`` or raise."""
        from payments.models import PaymentGatewayConfig

        if client is None:
            raise StripePaymentError(
                "Client context is required for Stripe key resolution."
            )

        config = PaymentGatewayConfig.objects.filter(
            client=client,
            provider="stripe",
            is_active=True,
        ).first()
        if not config:
            raise StripePaymentError(
                "Stripe is not configured or inactive for this client."
            )
        return config

    @staticmethod
    def _get_publishable_key(client: Client | None = None) -> str:
        """Get Stripe publishable key strictly from active client config."""
        config = StripePaymentService._get_active_stripe_config(client)
        key = config.get_publishable_key().strip()
        if not key:
            raise StripePaymentError(
                "Stripe publishable key is missing for this client."
            )
        return key

    @staticmethod
    def _persist_intent_snapshot(intent: Any) -> dict[str, Any]:
        from django.conf import settings

        from payments.stripe_payload_sanitize import maybe_persist_stripe_response

        return maybe_persist_stripe_response(
            intent,
            store_full=getattr(settings, "STORE_STRIPE_RAW_PAYLOADS", True),
        )

    @staticmethod
    def can_retry(payment: StripePayment) -> bool:
        """Check if a payment can be retried.

        Args:
            payment: StripePayment instance.

        Returns:
            bool: True if payment failed and retries remain.
        """
        return (
            payment.status == payment.STATUS_FAILED
            and payment.retry_count < payment.max_retries
        )

    @staticmethod
    def can_refund(payment: StripePayment) -> bool:
        """Check if a payment can be refunded.

        Args:
            payment: StripePayment instance.

        Returns:
            bool: True if payment succeeded and not fully refunded.
        """
        return (
            payment.status == payment.STATUS_SUCCEEDED
            and payment.amount_refunded < payment.amount
        )

    @staticmethod
    def _get_api_key(client: Client | None = None) -> str:
        """Get Stripe secret API key strictly from active client config."""
        config = StripePaymentService._get_active_stripe_config(client)
        key = config.get_secret_key().strip()
        if not key:
            raise StripePaymentError("Stripe secret key is missing for this client.")
        return key

    @staticmethod
    def get_or_create_customer(
        email: str,
        name: str,
        client: Client | None = None,
        donor: Donor | None = None,
        metadata: dict | None = None,
    ) -> StripeCustomer:
        """Get or create a Stripe customer.

        Args:
            email: Customer email address
            name: Customer full name
            client: Optional client (charity) link
            donor: Optional donor link
            metadata: Additional metadata

        Returns:
            StripeCustomer instance

        Raises:
            StripePaymentError: If Stripe API call fails
        """
        from payments.models import StripeCustomer

        # Check if customer already exists
        if client:
            existing = StripeCustomer.objects.filter(client=client).first()
            if existing:
                return existing
        elif donor:
            existing = StripeCustomer.objects.filter(donor=donor).first()
            if existing:
                return existing

        try:
            # Get API key
            api_key = StripePaymentService._get_api_key(client)

            # Create Stripe customer
            stripe_customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata=metadata or {},
                api_key=api_key,
            )
            record_payment_attempt(
                status="success", method="customer", client=_client_label(client)
            )

            # Save to database
            customer = StripeCustomer.objects.create(
                stripe_customer_id=stripe_customer.id,
                email=email,
                name=name,
                client=client,
                donor=donor,
                metadata=metadata or {},
            )

            logger.info("Created Stripe customer: %s", customer.stripe_customer_id)
            return customer

        except stripe.StripeError as e:
            record_payment_attempt(
                status="failure", method="customer", client=_client_label(client)
            )
            logger.error("Stripe customer creation failed: %s", e)
            raise StripePaymentError(f"Failed to create customer: {e!s}") from e

    @staticmethod
    def create_checkout_session(
        invoice: Invoice | None = None,
        donation: Donation | None = None,
        success_url: str = "",
        cancel_url: str = "",
        user: User | None = None,
    ) -> dict[str, Any]:
        """Create Stripe Checkout session for payment.

        Args:
            invoice: Invoice to pay
            donation: Donation to process
            success_url: URL to redirect after successful payment
            cancel_url: URL to redirect after cancelled payment
            user: User initiating payment

        Returns:
            Dict with session details including checkout URL

        Raises:
            StripePaymentError: If session creation fails
            ValidationError: If neither invoice nor donation provided
        """
        if not invoice and not donation:
            raise ValidationError("Either invoice or donation must be provided")

        # Resolve once up-front so both the success and failure metric
        # branches can label without re-deriving from invoice/donation.
        checkout_client = (
            invoice.client if invoice else donation.campaign.client  # pyright: ignore[reportOptionalMemberAccess]
        )

        try:
            # Determine amount and currency
            if invoice:
                amount = int(invoice.total_amount * 100)  # Convert to cents
                currency = "gbp"
                description = f"Invoice {invoice.invoice_number}"
                customer_email = invoice.client.email
                customer_name = invoice.client.name
                metadata = {
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.invoice_number,
                }
            else:  # donation
                assert (
                    donation is not None
                )  # guard: if not invoice, donation must exist
                assert (
                    donation.campaign is not None
                )  # a donation must belong to a campaign
                amount = int(donation.amount * 100)
                currency = donation.currency.lower()
                description = f"Donation to {donation.campaign.name}"
                customer_email = (
                    donation.system_donor.email
                    if donation.system_donor
                    else (
                        donation.donor.email
                        if donation.donor
                        else donation.data_file_donor.email
                    )
                )
                customer_name = (
                    f"{donation.system_donor.first_name} {donation.system_donor.last_name}"
                    if donation.system_donor
                    else (
                        f"{donation.donor.first_name} {donation.donor.last_name}"
                        if donation.donor
                        else (
                            f"{donation.data_file_donor.first_name} "
                            f"{donation.data_file_donor.last_name}"
                        )
                    )
                )
                metadata = {
                    "donation_id": str(donation.id),
                    "campaign_id": str(donation.campaign.id),
                }

            api_key = StripePaymentService._get_api_key(checkout_client)

            # Create checkout session
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": currency,
                            "product_data": {
                                "name": description,
                            },
                            "unit_amount": amount,
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=customer_email,
                metadata=metadata,
                api_key=api_key,
            )
            record_payment_attempt(
                status="success",
                method="checkout_session",
                client=_client_label(checkout_client),
            )

            # Create payment record
            customer = StripePaymentService.get_or_create_customer(
                email=customer_email,
                name=customer_name,
                client=invoice.client if invoice else None,
                donor=donation.donor if donation and donation.donor else None,
                metadata=metadata,
            )

            with transaction.atomic():
                from payments.models import StripePayment

                payment = StripePayment.objects.create(
                    stripe_payment_intent_id=session.payment_intent
                    if session.payment_intent
                    else f"pending_{session.id}",
                    stripe_checkout_session_id=session.id,
                    stripe_customer=customer,
                    invoice=invoice,
                    donation=donation,
                    amount=Decimal(amount) / 100,
                    currency=currency.upper(),
                    status=StripePayment.STATUS_PENDING,
                    description=description,
                    metadata=metadata,
                    processed_by=user,
                )

                # Update invoice/donation status
                if invoice:
                    invoice.payment_status = "processing"
                    invoice.save(update_fields=["payment_status"])
                if donation:
                    donation.payment_status = "processing"
                    donation.save(update_fields=["payment_status"])

            logger.info(
                "Created checkout session: %s for payment: %s", session.id, payment.id
            )

            return {
                "session_id": session.id,
                "checkout_url": session.url,
                "payment_id": str(payment.id),
                "amount": amount / 100,
                "currency": currency,
            }

        except stripe.StripeError as e:
            record_payment_attempt(
                status="failure",
                method="checkout_session",
                client=_client_label(checkout_client),
            )
            logger.error("Checkout session creation failed: %s", e)
            raise StripePaymentError(f"Failed to create checkout session: {e!s}") from e

    @staticmethod
    def send_authentication_link_for_donation(
        donation: Donation,
        success_url: str,
        cancel_url: str,
        user: User | None = None,
    ) -> dict[str, Any]:
        """Send a Stripe Checkout authentication link for an SCA-required donation.

        Used when ``BatchPaymentService._process_single_donation`` returned
        ``requires_action`` and the donation is sitting in
        ``payment_status="awaiting_authentication"``. Creates a fresh
        Stripe-hosted Checkout session so the donor can complete 3DS / SCA in
        their own browser, cancels the original PaymentIntent to release any
        authorisation hold, and emails the link to the donor. The local
        ``StripePayment`` row's ``stripe_payment_intent_id`` is repointed at
        the new Checkout-owned PaymentIntent so the existing
        ``payment_intent.succeeded`` webhook handler can find it on
        completion.

        Args:
            donation: Donation in ``awaiting_authentication`` payment status.
            success_url: URL the donor lands on after successful payment.
            cancel_url: URL the donor lands on if they cancel.
            user: Staff user dispatching the link (audit only).

        Returns:
            Dict with ``checkout_url``, ``session_id``, and ``payment_id``.

        Raises:
            ValidationError: When the donation is not awaiting authentication
                or has no recoverable StripePayment row.
            StripePaymentError: When the Stripe API call fails.
        """
        from django.core.mail import send_mail

        from payments.models import StripePayment

        if donation.payment_status != donation.PAYMENT_STATUS_AWAITING_AUTHENTICATION:
            raise ValidationError(
                "Donation is not awaiting authentication; cannot send link."
            )

        payment = (
            StripePayment.objects.filter(donation=donation)
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            raise ValidationError(
                "No StripePayment record exists for this donation; cannot "
                "send authentication link."
            )

        donor_email = ""
        donor_name = ""
        if donation.system_donor is not None:
            donor_email = donation.system_donor.email or ""
            donor_name = (
                f"{donation.system_donor.first_name} {donation.system_donor.last_name}"
            ).strip()
        elif donation.donor is not None:
            donor_email = donation.donor.email or ""
            donor_name = (
                f"{donation.donor.first_name} {donation.donor.last_name}".strip()
            )
        elif donation.data_file_donor is not None:
            donor_email = donation.data_file_donor.email or ""
            donor_name = (
                f"{donation.data_file_donor.first_name} "
                f"{donation.data_file_donor.last_name}"
            ).strip()

        if not donor_email:
            raise ValidationError(
                "Donor has no email address on file; cannot send authentication link."
            )

        api_key = StripePaymentService._get_api_key(donation.campaign.client)
        amount_cents = int(donation.amount * 100)
        currency = donation.currency.lower()
        description = f"Donation to {donation.campaign.name}"
        original_intent_id = payment.stripe_payment_intent_id

        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=donor_email,
                line_items=[
                    {
                        "price_data": {
                            "currency": currency,
                            "product_data": {"name": description},
                            "unit_amount": amount_cents,
                        },
                        "quantity": 1,
                    }
                ],
                metadata={
                    "donation_id": str(donation.id),
                    "campaign_id": str(donation.campaign.id),
                    "original_payment_intent_id": original_intent_id,
                    "recovery": "3ds_sca",
                },
                payment_intent_data={
                    "description": description,
                    "metadata": {
                        "donation_id": str(donation.id),
                        "campaign_id": str(donation.campaign.id),
                        "original_payment_intent_id": original_intent_id,
                        "recovery": "3ds_sca",
                    },
                },
                api_key=api_key,
            )
        except stripe.StripeError as exc:
            logger.error(
                "Failed to create authentication Checkout session for donation %s: %s",
                donation.id,
                exc,
            )
            raise StripePaymentError(
                f"Failed to create authentication link: {exc!s}"
            ) from exc

        new_intent_id = getattr(session, "payment_intent", None) or ""
        session_id = getattr(session, "id", "") or ""
        checkout_url = getattr(session, "url", "") or ""

        # Best-effort cancel of the original SCA-stuck intent so the donor's
        # bank releases the authorisation hold. Stripe rejects cancel on
        # already-succeeded intents — log and proceed if that happens.
        if original_intent_id:
            try:
                stripe.PaymentIntent.cancel(original_intent_id, api_key=api_key)
            except stripe.StripeError as exc:
                logger.warning(
                    "Could not cancel original SCA PaymentIntent %s: %s",
                    original_intent_id,
                    exc,
                )

        with transaction.atomic():
            update_fields: list[str] = [
                "stripe_checkout_session_id",
                "authentication_link_sent_at",
                "updated_at",
            ]
            payment.stripe_checkout_session_id = session_id
            payment.authentication_link_sent_at = timezone.now()
            if new_intent_id:
                # Re-point at the Checkout-owned intent so the existing
                # ``payment_intent.succeeded`` webhook lookup matches.
                payment.stripe_payment_intent_id = str(new_intent_id)
                update_fields.append("stripe_payment_intent_id")
            if user is not None and payment.processed_by_id is None:
                payment.processed_by = user
                update_fields.append("processed_by")
            payment.save(update_fields=update_fields)

        # Outbound email goes through the configured EMAIL_BACKEND so it
        # routes through Resend in prod, console in dev, locmem in tests.
        try:
            from django.conf import settings

            send_mail(
                subject=(
                    f"Action required: complete your donation to "
                    f"{donation.campaign.name}"
                ),
                message=(
                    f"Hello {donor_name or 'there'},\n\n"
                    f"Your card requires extra verification (3D Secure / SCA) "
                    f"before we can complete your donation of "
                    f"{donation.currency} {donation.amount:.2f} to "
                    f"{donation.campaign.name}.\n\n"
                    f"Please follow the secure link below to authenticate "
                    f"the payment with your bank. The link is unique to "
                    f"this donation and will expire shortly:\n\n"
                    f"{checkout_url}\n\n"
                    f"If you have already completed this payment you can "
                    f"safely ignore this email.\n\n"
                    f"Thank you for your support."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[donor_email],
                fail_silently=False,
            )
        except Exception as exc:
            # Email failure must not roll back the Checkout session — staff
            # can still copy the URL from the response and resend manually.
            logger.exception(
                "Failed to email authentication link for donation %s: %s",
                donation.id,
                exc,
            )

        logger.info(
            "Sent 3DS/SCA authentication link for donation %s "
            "(session=%s, new_intent=%s, original_intent=%s)",
            donation.id,
            session_id,
            new_intent_id,
            original_intent_id,
        )

        return {
            "session_id": session_id,
            "checkout_url": checkout_url,
            "payment_id": str(payment.id),
            "donor_email": donor_email,
        }

    @staticmethod
    def _apply_successful_payment(
        payment: StripePayment, intent: stripe.PaymentIntent
    ) -> StripePayment | None:
        """Apply a confirmed Stripe intent to local DB objects inside a DB transaction.

        Re-fetches the payment with a row-level lock on PostgreSQL before writing
        to handle concurrent webhook deliveries.  Returns None if a concurrent
        worker already processed this payment.

        Args:
            payment: StripePayment fetched before calling this helper.
            intent: Stripe PaymentIntent object retrieved from the API.

        Returns:
            Updated StripePayment or None when a concurrent worker beat us.
        """
        from django.db import connection as db_conn

        from payments.models import StripePayment as _StripePayment

        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = _StripePayment.objects.select_for_update().get(
                    stripe_payment_intent_id=payment.stripe_payment_intent_id
                )

            # Second guard inside the lock: another worker may have succeeded
            # between the pre-flight check and acquiring this lock.
            if payment.status == _StripePayment.STATUS_SUCCEEDED:
                logger.info(
                    "Payment %s already succeeded (concurrent worker) — skipping",
                    payment.stripe_payment_intent_id,
                )
                return None

            payment.status = _StripePayment.STATUS_SUCCEEDED
            payment.stripe_charge_id = (
                intent.latest_charge if intent.latest_charge else None
            )
            payment.stripe_response = StripePaymentService._persist_intent_snapshot(
                intent
            )
            payment.error_message = ""
            payment.error_code = ""
            payment.save()

            if payment.invoice:
                payment.invoice.payment_status = "paid"
                payment.invoice.status = "paid"
                payment.invoice.payment_date = timezone.now().date()
                payment.invoice.amount_paid = payment.amount
                payment.invoice.balance_due = 0
                payment.invoice.save()

            if payment.donation:
                payment.donation.payment_status = "completed"
                payment.donation.donation_date = timezone.now().date()
                payment.donation.save()

        return payment

    @staticmethod
    def process_successful_payment(payment_intent_id: str) -> StripePayment | None:
        """Process successful payment from webhook.

        Idempotent: safe to call multiple times for the same payment intent.
        On the second call (Celery retry or concurrent webhook delivery) the
        payment will already be STATUS_SUCCEEDED and the method returns early
        without re-updating the invoice.

        Args:
            payment_intent_id: Stripe payment intent ID

        Returns:
            Updated StripePayment instance or None
        """
        from payments.models import StripePayment

        try:
            payment = StripePayment.objects.get(
                stripe_payment_intent_id=payment_intent_id
            )

            # Pre-flight idempotency guard: avoids an unnecessary Stripe API call
            # when the task is retried or Stripe delivers the same event twice.
            if payment.status == StripePayment.STATUS_SUCCEEDED:
                logger.info(
                    "Payment %s already succeeded — skipping duplicate processing",
                    payment_intent_id,
                )
                return payment

            # Network call must run outside the DB transaction.
            api_key = StripePaymentService._get_api_key(payment.stripe_customer.client)
            try:
                intent = stripe.PaymentIntent.retrieve(
                    payment_intent_id, api_key=api_key
                )
            except stripe.StripeError:
                record_payment_attempt(
                    status="failure",
                    method="payment_intent_retrieve",
                    client=_client_label(payment.stripe_customer.client),
                )
                raise
            record_payment_attempt(
                status="success",
                method="payment_intent_retrieve",
                client=_client_label(payment.stripe_customer.client),
            )

            result = StripePaymentService._apply_successful_payment(payment, intent)
            # result is None when a concurrent worker already processed this payment
            applied = result if result is not None else payment
            logger.info("Processed successful payment: %s", applied.id)
            return applied

        except StripePayment.DoesNotExist:
            logger.warning("Payment not found for intent: %s", payment_intent_id)
            return None
        except Exception as e:
            logger.error("Failed to process successful payment: %s", e)
            raise

    @staticmethod
    def process_failed_payment(
        payment_intent_id: str, error_message: str = "", error_code: str = ""
    ) -> StripePayment | None:
        """Process failed payment from webhook.

        Args:
            payment_intent_id: Stripe payment intent ID
            error_message: Error message from Stripe
            error_code: Error code from Stripe

        Returns:
            Updated StripePayment instance or None
        """
        from payments.models import StripePayment

        try:
            payment = StripePayment.objects.get(
                stripe_payment_intent_id=payment_intent_id
            )

            with transaction.atomic():
                # Update payment status
                payment.status = StripePayment.STATUS_FAILED
                payment.error_message = error_message
                payment.error_code = error_code
                payment.retry_count += 1

                # Schedule retry if eligible
                if StripePaymentService.can_retry(payment):
                    # Exponential backoff: 1hr, 6hr, 24hr
                    retry_delays = [1, 6, 24]
                    delay_hours = retry_delays[
                        min(payment.retry_count - 1, len(retry_delays) - 1)
                    ]
                    payment.next_retry_at = timezone.now() + timedelta(
                        hours=delay_hours
                    )

                payment.last_retry_at = timezone.now()
                payment.save()

                # Update invoice/donation status
                if payment.invoice:
                    payment.invoice.payment_status = "failed"
                    payment.invoice.save(update_fields=["payment_status"])

                if payment.donation:
                    payment.donation.payment_status = "failed"
                    payment.donation.save(update_fields=["payment_status"])

            logger.info("Processed failed payment: %s", payment.id)
            return payment

        except StripePayment.DoesNotExist:
            logger.warning("Payment not found for intent: %s", payment_intent_id)
            return None
        except Exception as e:
            logger.error("Failed to process failed payment: %s", e)
            raise

    @staticmethod
    def refund_payment(
        payment_id: str, amount: Decimal | None = None, reason: str = ""
    ) -> dict[str, Any]:
        """Refund a successful payment.

        Concurrency: ``amount_refunded`` is also written by the
        ``charge.refunded`` webhook handler (``process_charge_refunded``). Both
        paths must row-lock the payment via ``select_for_update`` before
        reading ``amount_refunded`` to compute the new total — otherwise an
        admin refund firing simultaneously with the inbound webhook can lose
        an update. The Stripe API call is also issued with an idempotency key
        derived from ``(payment.id, amount_cents, reason)`` so a transparent
        retry on a network blip can't double-charge a refund.

        Args:
            payment_id: Payment UUID
            amount: Optional partial refund amount
            reason: Reason for refund

        Returns:
            Dict with refund details

        Raises:
            ValidationError: If payment cannot be refunded
            StripePaymentError: If Stripe API call fails
        """
        from django.db import connection as db_conn

        from payments.models import StripePayment

        client_for_attempt = None
        try:
            # Phase 1: acquire row lock, validate, capture state, release lock.
            with transaction.atomic():
                if db_conn.vendor != "sqlite":
                    payment = StripePayment.objects.select_for_update().get(
                        id=payment_id
                    )
                else:
                    payment = StripePayment.objects.get(id=payment_id)

                if not StripePaymentService.can_refund(payment):
                    raise ValidationError("Payment cannot be refunded")

                refund_amount = (
                    amount if amount else payment.amount - payment.amount_refunded
                )
                refund_amount_cents = int(refund_amount * 100)
                api_key = StripePaymentService._get_api_key(
                    payment.stripe_customer.client
                )
                payment_intent_id = payment.stripe_payment_intent_id
                amount_refunded_at_read = payment.amount_refunded
                client_for_attempt = payment.stripe_customer.client

            # Idempotency key derived from payment + amount + reason so retries
            # of the *same* refund request dedupe server-side, but a follow-up
            # refund (different amount or reason) gets a fresh key and is
            # treated as a separate refund by Stripe.
            reason_hash = hashlib.sha256(
                f"{refund_amount_cents}:{reason}".encode()
            ).hexdigest()[:16]
            idempotency_key = f"refund_pmt_{payment.id}_{reason_hash}"

            # Phase 2: Stripe API call OUTSIDE the row lock so a stalled
            # network call cannot pile up DB connections.
            refund = stripe.Refund.create(
                payment_intent=payment_intent_id,
                amount=refund_amount_cents,
                reason="requested_by_customer" if not reason else None,  # pyright: ignore[reportArgumentType]
                metadata={"refund_reason": reason},
                api_key=api_key,
                idempotency_key=idempotency_key,
            )
            record_payment_attempt(
                status="success",
                method="refund",
                client=_client_label(client_for_attempt),
            )

            # Phase 3: re-lock + write result. Idempotent against an inbound
            # ``charge.refunded`` webhook that may have already accounted for
            # this refund delta during the Stripe call.
            with transaction.atomic():
                if db_conn.vendor != "sqlite":
                    payment = StripePayment.objects.select_for_update().get(
                        id=payment_id
                    )
                else:
                    payment = StripePayment.objects.get(id=payment_id)

                if payment.status == StripePayment.STATUS_REFUNDED:
                    return {
                        "refund_id": refund.id,
                        "amount": refund_amount,
                        "status": refund.status,
                        "payment_id": str(payment.id),
                    }
                if payment.amount_refunded >= amount_refunded_at_read + refund_amount:
                    return {
                        "refund_id": refund.id,
                        "amount": refund_amount,
                        "status": refund.status,
                        "payment_id": str(payment.id),
                    }

                payment.amount_refunded += refund_amount
                payment.refund_reason = reason

                if payment.amount_refunded >= payment.amount:
                    payment.status = StripePayment.STATUS_REFUNDED
                else:
                    payment.status = StripePayment.STATUS_PARTIALLY_REFUNDED

                payment.save()

                if payment.invoice:
                    payment.invoice.payment_status = "refunded"
                    payment.invoice.status = "cancelled"
                    payment.invoice.save()

                if payment.donation:
                    payment.donation.payment_status = "refunded"
                    payment.donation.save()

            logger.info(
                "Refunded %s from payment: %s, refund ID: %s",
                refund_amount,
                payment.id,
                refund.id,
            )

            return {
                "refund_id": refund.id,
                "amount": refund_amount,
                "status": refund.status,
                "payment_id": str(payment.id),
            }

        except StripePayment.DoesNotExist:
            raise ValidationError("Payment not found") from None
        except stripe.StripeError as e:
            # StripeError fires from ``stripe.Refund.create`` in phase 2, after
            # phase 1 has already captured ``client_for_attempt``. Use the
            # captured value rather than re-dereferencing ``payment`` so
            # pyright can prove the local is always bound.
            record_payment_attempt(
                status="failure",
                method="refund",
                client=_client_label(client_for_attempt),
            )
            logger.error("Refund failed: %s", e)
            raise StripePaymentError(f"Failed to process refund: {e!s}") from e

    @staticmethod
    def capture_payment_intent(stripe_payment_id: str) -> dict[str, Any]:
        """Capture a previously-authorised PaymentIntent (MOTO settlement).

        Called from the QA approval path for phone-intake card donations
        whose ``payment_status`` is ``requires_capture``. On success, flips
        the related ``StripePayment.status`` to ``succeeded`` and the
        ``Donation.payment_status`` to ``completed`` and enqueues the
        deferred (post-charge) redaction. On Stripe error (most often
        because the auth has expired past its 7-day window), records the
        failure on ``StripePaymentAttempt`` and flips the donation to
        ``failed`` so QA can collect a fresh auth.

        Concurrency: matches :meth:`refund_payment`'s three-phase pattern
        — phase 1 acquires a row lock, validates the status, and releases
        the lock; phase 2 issues the Stripe API call outside the lock so
        a stalled network call cannot pile up DB connections; phase 3
        re-locks and writes the outcome with an idempotency guard against
        a concurrent worker that already flipped the row. The Stripe
        idempotency key is derived from ``(payment.id, "capture")`` so a
        network-level retry replays the same capture rather than
        double-settling.

        Args:
            stripe_payment_id: ``StripePayment.id`` (UUID) — not the
                Stripe-side ``pi_*`` identifier.

        Returns:
            Dict with the capture outcome:
              ``{"success": True, "payment_id": ..., "message": ...}`` or
              ``{"success": False, "error": ...}``.
        """
        from django.db import connection as db_conn

        from donations.models import Donation
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment, StripePaymentAttempt

        # Phase 1: lock, validate, capture context, release.
        try:
            with transaction.atomic():
                if db_conn.vendor != "sqlite":
                    payment = (
                        StripePayment.objects.select_for_update()
                        .select_related("donation", "donation__campaign__client")
                        .get(id=stripe_payment_id)
                    )
                else:
                    payment = StripePayment.objects.select_related(
                        "donation", "donation__campaign__client"
                    ).get(id=stripe_payment_id)

                donation = payment.donation
                if donation is None:
                    return {
                        "success": False,
                        "error": "Payment has no associated donation",
                    }

                if payment.status == StripePayment.STATUS_SUCCEEDED:
                    return {
                        "success": True,
                        "skipped": True,
                        "message": "Payment already captured",
                    }
                if payment.status != StripePayment.STATUS_REQUIRES_CAPTURE:
                    return {
                        "success": False,
                        "error": (
                            f"Payment status {payment.status!r} cannot be captured "
                            "(must be requires_capture)"
                        ),
                    }

                api_key = StripePaymentService._get_api_key(donation.campaign.client)
                payment_intent_id = payment.stripe_payment_intent_id
                amount_cents = int(donation.amount * 100)
                currency = donation.currency
                payment_pk = payment.pk
        except StripePayment.DoesNotExist:
            return {"success": False, "error": "StripePayment not found"}

        idempotency_key = f"capture_{payment_pk}"

        # Phase 2: Stripe API call OUTSIDE the row lock.
        try:
            captured = stripe.PaymentIntent.capture(
                payment_intent_id,
                api_key=api_key,
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            logger.error(
                "Capture failed for StripePayment %s (donation %s): %s",
                payment_pk,
                donation.id,
                exc,
            )
            error_code = getattr(exc, "code", "") or ""
            with transaction.atomic():
                if db_conn.vendor != "sqlite":
                    payment = StripePayment.objects.select_for_update().get(
                        pk=payment_pk
                    )
                else:
                    payment = StripePayment.objects.get(pk=payment_pk)
                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency,
                    status=StripePaymentAttempt.STATUS_FAILED,
                    attempt_number=StripePaymentAttempt.objects.next_attempt_number(
                        donation
                    ),
                    stripe_payment_intent_id=payment_intent_id,
                    error_code=str(error_code),
                    error_message=str(exc),
                )
                payment.status = StripePayment.STATUS_FAILED
                payment.save(update_fields=["status"])
                donation.payment_status = Donation.PAYMENT_STATUS_FAILED
                donation.save(update_fields=["payment_status"])
            return {"success": False, "error": str(exc)}

        intent_status = getattr(captured, "status", "") or ""
        latest_charge = getattr(captured, "latest_charge", "") or ""

        # Phase 3: re-lock and write the outcome. A concurrent worker may
        # have already promoted the row to SUCCEEDED via the same
        # idempotency key — short-circuit when that happens.
        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = StripePayment.objects.select_for_update().get(pk=payment_pk)
            else:
                payment = StripePayment.objects.get(pk=payment_pk)

            if payment.status == StripePayment.STATUS_SUCCEEDED:
                logger.info(
                    "Capture for StripePayment %s already applied by concurrent "
                    "worker — idempotent skip",
                    payment_pk,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "payment_id": str(payment.id),
                    "message": "Payment already captured",
                }

            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=amount_cents,
                currency=currency,
                status=StripePaymentAttempt.STATUS_SUCCEEDED,
                attempt_number=StripePaymentAttempt.objects.next_attempt_number(
                    donation
                ),
                stripe_payment_intent_id=payment_intent_id,
            )

            if isinstance(latest_charge, str) and latest_charge:
                payment.stripe_charge_id = latest_charge
            payment.status = StripePayment.STATUS_SUCCEEDED
            payment.stripe_response = StripePaymentService._persist_intent_snapshot(
                captured
            )
            payment.save(
                update_fields=["status", "stripe_charge_id", "stripe_response"]
            )

            donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
            donation.save(update_fields=["payment_status"])

        BatchPaymentService._enqueue_deferred_redaction(donation)

        logger.info(
            "Captured StripePayment %s for donation %s (intent_status=%s)",
            payment_pk,
            donation.id,
            intent_status,
        )
        return {
            "success": True,
            "payment_id": str(payment_pk),
            "message": "Payment captured",
        }

    @staticmethod
    def cancel_payment_intent(
        stripe_payment_id: str, reason: str = "qa_rejected"
    ) -> dict[str, Any]:
        """Cancel an authorised PaymentIntent (MOTO QA-reject path).

        Called when QA rejects a phone-intake card donation: releases the
        hold on the donor's card without an explicit refund cycle (the
        funds were never settled). On success, flips
        ``StripePayment.status`` → ``cancelled`` and
        ``Donation.payment_status`` → ``failed``.

        Concurrency: matches :meth:`refund_payment` / :meth:`capture_payment_intent`
        — phase 1 locks/validates/releases, phase 2 issues the Stripe call
        outside the lock, phase 3 re-locks and writes idempotently. The
        Stripe idempotency key is derived from ``(payment.id, "cancel")``.

        Args:
            stripe_payment_id: ``StripePayment.id`` (UUID).
            reason: Cancellation reason recorded in attempt audit + Stripe.

        Returns:
            Dict with the cancellation outcome.
        """
        from django.db import connection as db_conn

        from donations.models import Donation
        from payments.models import StripePayment, StripePaymentAttempt

        # Phase 1: lock, validate, capture context, release.
        try:
            with transaction.atomic():
                if db_conn.vendor != "sqlite":
                    payment = (
                        StripePayment.objects.select_for_update()
                        .select_related("donation", "donation__campaign__client")
                        .get(id=stripe_payment_id)
                    )
                else:
                    payment = StripePayment.objects.select_related(
                        "donation", "donation__campaign__client"
                    ).get(id=stripe_payment_id)

                donation = payment.donation
                if donation is None:
                    return {
                        "success": False,
                        "error": "Payment has no associated donation",
                    }

                if payment.status == StripePayment.STATUS_CANCELLED:
                    return {
                        "success": True,
                        "skipped": True,
                        "message": "Payment already cancelled",
                    }
                if payment.status != StripePayment.STATUS_REQUIRES_CAPTURE:
                    return {
                        "success": False,
                        "error": (
                            f"Payment status {payment.status!r} cannot be cancelled "
                            "(must be requires_capture)"
                        ),
                    }

                api_key = StripePaymentService._get_api_key(donation.campaign.client)
                payment_intent_id = payment.stripe_payment_intent_id
                amount_cents = int(donation.amount * 100)
                currency = donation.currency
                payment_pk = payment.pk
        except StripePayment.DoesNotExist:
            return {"success": False, "error": "StripePayment not found"}

        idempotency_key = f"cancel_{payment_pk}"

        # Phase 2: Stripe API call OUTSIDE the row lock.
        try:
            stripe.PaymentIntent.cancel(
                payment_intent_id,
                cancellation_reason="requested_by_customer",
                api_key=api_key,
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            logger.error(
                "Cancel failed for StripePayment %s (donation %s): %s",
                payment_pk,
                donation.id,
                exc,
            )
            error_code = getattr(exc, "code", "") or ""
            with transaction.atomic():
                StripePaymentAttempt.objects.log_attempt(
                    donation=donation,
                    amount_cents=amount_cents,
                    currency=currency,
                    status=StripePaymentAttempt.STATUS_FAILED,
                    attempt_number=StripePaymentAttempt.objects.next_attempt_number(
                        donation
                    ),
                    stripe_payment_intent_id=payment_intent_id,
                    error_code=str(error_code),
                    error_message=str(exc),
                )
            return {"success": False, "error": str(exc)}

        # Phase 3: re-lock and write the outcome. A concurrent worker may
        # have already cancelled this row via the same idempotency key.
        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = StripePayment.objects.select_for_update().get(pk=payment_pk)
            else:
                payment = StripePayment.objects.get(pk=payment_pk)

            if payment.status == StripePayment.STATUS_CANCELLED:
                logger.info(
                    "Cancel for StripePayment %s already applied by concurrent "
                    "worker — idempotent skip",
                    payment_pk,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "payment_id": str(payment.id),
                    "message": "Payment already cancelled",
                }

            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=amount_cents,
                currency=currency,
                status=StripePaymentAttempt.STATUS_SUCCEEDED,
                attempt_number=StripePaymentAttempt.objects.next_attempt_number(
                    donation
                ),
                stripe_payment_intent_id=payment_intent_id,
                error_message=f"Cancelled: {reason}",
            )

            payment.status = StripePayment.STATUS_CANCELLED
            payment.save(update_fields=["status"])
            donation.payment_status = Donation.PAYMENT_STATUS_FAILED
            donation.save(update_fields=["payment_status"])

        logger.info(
            "Cancelled StripePayment %s for donation %s (reason=%s)",
            payment_pk,
            donation.id,
            reason,
        )
        return {
            "success": True,
            "payment_id": str(payment_pk),
            "message": "Payment authorisation cancelled",
        }

    @staticmethod
    def retry_failed_payment(payment_id: str) -> dict[str, Any]:
        """Retry a failed payment.

        Args:
            payment_id: Payment UUID

        Returns:
            Dict with retry status

        Raises:
            ValidationError: If payment cannot be retried
        """
        from payments.models import StripePayment

        try:
            payment = StripePayment.objects.get(id=payment_id)

            if not StripePaymentService.can_retry(payment):
                raise ValidationError(
                    "Payment cannot be retried (either not failed or max retries reached)"
                )

            # Get API key for the associated client
            client = None
            if payment.donation and payment.donation.campaign:
                client = payment.donation.campaign.client
            api_key = StripePaymentService._get_api_key(client)

            # Attempt to retry via Stripe by creating a new PaymentIntent
            if payment.stripe_payment_intent_id:
                try:
                    # Retrieve the original payment intent
                    original_pi = stripe.PaymentIntent.retrieve(
                        payment.stripe_payment_intent_id,
                        api_key=api_key,
                    )

                    # Idempotency key keyed on payment + retry_count so a
                    # mid-retry network failure that triggers an SDK-level
                    # request retry dedupes against the same Stripe call,
                    # while the *next* manual retry attempt gets a fresh key.
                    retry_idempotency_key = (
                        f"pmt_{payment.id}_retry_{payment.retry_count}"
                    )

                    # Create a new payment intent with the same parameters
                    new_pi = stripe.PaymentIntent.create(
                        amount=original_pi.amount,
                        currency=original_pi.currency,
                        customer=original_pi.customer,  # pyright: ignore[reportArgumentType]
                        description=original_pi.description or "",
                        metadata=original_pi.metadata,
                        confirm=True,
                        automatic_payment_methods={
                            "enabled": True,
                            "allow_redirects": "never",
                        },
                        api_key=api_key,
                        idempotency_key=retry_idempotency_key,
                    )
                    record_payment_attempt(
                        status=(
                            "success"
                            if new_pi.status == "succeeded"
                            else new_pi.status or "pending"
                        ),
                        method="payment_intent_retry",
                        client=_client_label(client),
                    )

                    # Update payment record with new intent
                    payment.stripe_payment_intent_id = new_pi.id
                    payment.stripe_response = (
                        StripePaymentService._persist_intent_snapshot(new_pi)
                    )
                    payment.retry_count += 1
                    payment.next_retry_at = None

                    if new_pi.status == "succeeded":
                        payment.status = StripePayment.STATUS_SUCCEEDED
                        payment.stripe_charge_id = (
                            new_pi.latest_charge if new_pi.latest_charge else None
                        )
                        if payment.donation:
                            payment.donation.payment_status = "completed"
                            payment.donation.save(update_fields=["payment_status"])
                    else:
                        payment.status = StripePayment.STATUS_PENDING

                    payment.save()

                    logger.info(
                        "Retried payment %s with new intent %s", payment.id, new_pi.id
                    )

                    return {
                        "payment_id": str(payment.id),
                        "status": "succeeded"
                        if new_pi.status == "succeeded"
                        else "retry_scheduled",
                        "retry_count": payment.retry_count,
                    }

                except stripe.StripeError as e:
                    record_payment_attempt(
                        status="failure",
                        method="payment_intent_retry",
                        client=_client_label(client),
                    )
                    logger.error(
                        "Stripe retry failed for payment %s: %s", payment.id, e
                    )
                    payment.retry_count += 1
                    payment.error_message = str(e)
                    payment.next_retry_at = timezone.now() + timedelta(hours=1)
                    payment.save()
                    raise StripePaymentError(f"Retry failed: {e!s}") from e
            else:
                # No original payment intent — just reset status
                payment.status = StripePayment.STATUS_PENDING
                payment.next_retry_at = None
                payment.save()

            logger.info("Scheduled retry for payment: %s", payment.id)

            return {
                "payment_id": str(payment.id),
                "status": "retry_scheduled",
                "retry_count": payment.retry_count,
            }

        except StripePayment.DoesNotExist:
            raise ValidationError("Payment not found") from None

    @staticmethod
    def _donation_payment_status_for_stripe_status(stripe_status: str) -> str:
        """Map a ``StripePayment.status`` value to a ``Donation.payment_status``.

        ``Donation.payment_status`` has fewer states than ``StripePayment.status``;
        partial-refund and pending-on-payment states collapse to the closest
        donation-level meaning.

        Args:
            stripe_status: A value from ``StripePayment.STATUS_CHOICES``.

        Returns:
            The matching donation ``payment_status`` choice. Falls back to
            ``"pending"`` for unknown values.
        """
        from payments.models import StripePayment as _StripePayment

        mapping: dict[str, str] = {
            _StripePayment.STATUS_PENDING: "pending",
            _StripePayment.STATUS_PROCESSING: "processing",
            _StripePayment.STATUS_SUCCEEDED: "completed",
            _StripePayment.STATUS_FAILED: "failed",
            _StripePayment.STATUS_REFUNDED: "refunded",
            # Donation has no "partially_refunded" state — surface partial
            # refunds as "refunded" so reporting flags the donation for review.
            _StripePayment.STATUS_PARTIALLY_REFUNDED: "refunded",
            _StripePayment.STATUS_CANCELLED: "failed",
            _StripePayment.STATUS_DISPUTED: "disputed",
            _StripePayment.STATUS_DISPUTE_LOST: "dispute_lost",
        }
        return mapping.get(stripe_status, "pending")

    @staticmethod
    def _find_payment_by_charge(
        charge_id: str | None, payment_intent_id: str | None
    ) -> StripePayment | None:
        """Locate the local payment for a Stripe charge or intent reference.

        Stripe ``charge.*`` events carry the charge ID in ``data.object.id`` and
        usually the payment intent in ``data.object.payment_intent`` — try both.

        Args:
            charge_id: Stripe charge ID (``ch_...``).
            payment_intent_id: Stripe payment intent ID (``pi_...``).

        Returns:
            Matching StripePayment instance or None.
        """
        from payments.models import StripePayment

        if charge_id:
            payment = StripePayment.objects.filter(stripe_charge_id=charge_id).first()
            if payment is not None:
                return payment
        if payment_intent_id:
            return StripePayment.objects.filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()
        return None

    @staticmethod
    def _notify_staff_of_payment_event(
        payment: StripePayment,
        title: str,
        message: str,
        notification_type: str,
    ) -> None:
        """Create one in-app Notification per active staff user.

        Failures here must never break webhook processing — a missing signal
        is preferable to losing a Stripe event.

        Args:
            payment: The affected StripePayment.
            title: Short notification title.
            message: Full notification message.
            notification_type: One of ``Notification.NOTIFICATION_TYPE_CHOICES``.
        """
        try:
            from core.models import User
            from notifications.models import Notification

            staff_users = User.objects.filter(is_staff=True, is_active=True)
            notifications = [
                Notification(
                    user=user,
                    title=title,
                    message=message,
                    notification_type=notification_type,
                    related_object_type="StripePayment",
                    related_object_id=str(payment.id),
                )
                for user in staff_users
            ]
            if notifications:
                Notification.objects.bulk_create(notifications)
        except Exception as exc:
            logger.exception("Failed to create staff notification: %s", exc)

    @staticmethod
    def process_charge_refunded(event_data: dict[str, Any]) -> StripePayment | None:
        """Apply a Stripe ``charge.refunded`` event to the local payment.

        Idempotent: if the payment is already in the terminal
        ``STATUS_REFUNDED`` state, the call short-circuits without writing.
        Otherwise the new ``amount_refunded`` is stored and the status is
        recomputed against ``payment.amount`` — this lets a later webhook that
        bumps a partial refund up to a full refund correctly promote
        ``STATUS_PARTIALLY_REFUNDED`` to ``STATUS_REFUNDED``.

        Args:
            event_data: ``data.object`` dict from the Stripe event payload.

        Returns:
            Updated StripePayment.

        Raises:
            StripePaymentNotFoundError: When no local payment matches the
                charge/intent on the event. The Celery webhook task converts
                this into a retry so the refund is not silently dropped if
                ``payment_intent.succeeded`` is still racing to persist.
        """
        from django.db import connection as db_conn

        from payments.models import StripePayment

        charge_id = event_data.get("id")
        payment_intent_id = event_data.get("payment_intent")
        amount_refunded_cents = int(event_data.get("amount_refunded", 0) or 0)
        amount_refunded = Decimal(amount_refunded_cents) / Decimal(100)

        payment = StripePaymentService._find_payment_by_charge(
            charge_id, payment_intent_id
        )
        if payment is None:
            logger.warning(
                "charge.refunded received with no matching payment "
                "(charge=%s intent=%s) — will retry",
                charge_id,
                payment_intent_id,
            )
            raise StripePaymentNotFoundError(
                f"No StripePayment found for charge={charge_id} "
                f"intent={payment_intent_id}"
            )

        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = StripePayment.objects.select_for_update().get(pk=payment.pk)

            # Short-circuit ONLY when the payment is already fully refunded.
            # A partial-refund row must still be allowed to promote to fully
            # refunded when a follow-up webhook bumps amount_refunded.
            if payment.status == StripePayment.STATUS_REFUNDED:
                logger.info("Payment %s already refunded — idempotent skip", payment.id)
                return payment

            payment.amount_refunded = amount_refunded
            if amount_refunded >= payment.amount:
                payment.status = StripePayment.STATUS_REFUNDED
            else:
                payment.status = StripePayment.STATUS_PARTIALLY_REFUNDED
            payment.save(update_fields=["amount_refunded", "status", "updated_at"])

            donation_status = (
                StripePaymentService._donation_payment_status_for_stripe_status(
                    payment.status
                )
            )
            if payment.donation:
                payment.donation.payment_status = donation_status
                payment.donation.save(update_fields=["payment_status"])
            if payment.invoice:
                payment.invoice.payment_status = "refunded"
                payment.invoice.save(update_fields=["payment_status"])

        from notifications.models import Notification

        StripePaymentService._notify_staff_of_payment_event(
            payment=payment,
            title="Stripe refund processed",
            message=(
                f"{payment.currency} {amount_refunded} refunded on payment "
                f"{payment.stripe_payment_intent_id}."
            ),
            notification_type=Notification.TYPE_WARNING,
        )
        logger.info("Processed charge.refunded for payment %s", payment.id)
        return payment

    @staticmethod
    def process_charge_dispute_created(
        event_data: dict[str, Any],
    ) -> StripePayment | None:
        """Apply a Stripe ``charge.dispute.created`` event.

        Captures the prior status into ``pre_dispute_status`` so a won dispute
        can later restore the original state, then flips both the payment and
        donation to ``"disputed"``.

        Args:
            event_data: ``data.object`` dict from the Stripe event payload
                (the dispute object).

        Returns:
            Updated StripePayment.

        Raises:
            StripePaymentNotFoundError: When no local payment matches the
                charge/intent on the event. The Celery webhook task converts
                this into a retry so the dispute is not silently dropped if
                ``payment_intent.succeeded`` is still racing to persist.
        """
        from django.db import connection as db_conn

        from payments.models import StripePayment

        charge_id = event_data.get("charge")
        payment_intent_id = event_data.get("payment_intent")
        reason = str(event_data.get("reason", "") or "")[:100]
        amount_cents = int(event_data.get("amount", 0) or 0)
        amount = Decimal(amount_cents) / Decimal(100)
        currency = str(event_data.get("currency", "gbp") or "gbp").upper()

        payment = StripePaymentService._find_payment_by_charge(
            charge_id, payment_intent_id
        )
        if payment is None:
            logger.warning(
                "charge.dispute.created received with no matching payment "
                "(charge=%s intent=%s) — will retry",
                charge_id,
                payment_intent_id,
            )
            raise StripePaymentNotFoundError(
                f"No StripePayment found for charge={charge_id} "
                f"intent={payment_intent_id}"
            )

        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = StripePayment.objects.select_for_update().get(pk=payment.pk)

            if payment.status == StripePayment.STATUS_DISPUTED:
                logger.info("Payment %s already disputed — idempotent skip", payment.id)
                return payment

            if not payment.pre_dispute_status:
                payment.pre_dispute_status = payment.status
            payment.status = StripePayment.STATUS_DISPUTED
            payment.dispute_reason = reason
            payment.save(
                update_fields=[
                    "status",
                    "pre_dispute_status",
                    "dispute_reason",
                    "updated_at",
                ]
            )

            if payment.donation:
                payment.donation.payment_status = "disputed"
                payment.donation.save(update_fields=["payment_status"])

        from notifications.models import Notification

        StripePaymentService._notify_staff_of_payment_event(
            payment=payment,
            title="Stripe dispute opened",
            message=(
                f"Dispute opened on {payment.stripe_payment_intent_id} "
                f"({currency} {amount}). Reason: {reason or 'unspecified'}."
            ),
            notification_type=Notification.TYPE_ERROR,
        )
        logger.info("Processed charge.dispute.created for payment %s", payment.id)
        return payment

    @staticmethod
    def process_charge_dispute_closed(
        event_data: dict[str, Any],
    ) -> StripePayment | None:
        """Apply a Stripe ``charge.dispute.closed`` event.

        On ``status="won"`` (rebuttal accepted) the prior payment status is
        restored from ``pre_dispute_status`` and the donation
        ``payment_status`` is mapped back to its closest non-disputed
        equivalent (e.g. PARTIALLY_REFUNDED restores donation to
        ``"refunded"``). On ``status="lost"`` the payment is moved to a
        terminal ``dispute_lost`` state and the donation is marked
        ``"dispute_lost"``. Other outcomes (``warning_*``) are logged but
        leave the disputed status in place.

        Args:
            event_data: ``data.object`` dict from the Stripe event payload.

        Returns:
            Updated StripePayment.

        Raises:
            StripePaymentNotFoundError: When no local payment matches the
                charge/intent on the event. The Celery webhook task converts
                this into a retry.
        """
        from django.db import connection as db_conn

        from notifications.models import Notification
        from payments.models import StripePayment

        charge_id = event_data.get("charge")
        payment_intent_id = event_data.get("payment_intent")
        outcome = str(event_data.get("status", "") or "").lower()

        payment = StripePaymentService._find_payment_by_charge(
            charge_id, payment_intent_id
        )
        if payment is None:
            logger.warning(
                "charge.dispute.closed received with no matching payment "
                "(charge=%s intent=%s) — will retry",
                charge_id,
                payment_intent_id,
            )
            raise StripePaymentNotFoundError(
                f"No StripePayment found for charge={charge_id} "
                f"intent={payment_intent_id}"
            )

        with transaction.atomic():
            if db_conn.vendor != "sqlite":
                payment = StripePayment.objects.select_for_update().get(pk=payment.pk)

            if outcome == "won":
                restored_status = (
                    payment.pre_dispute_status or StripePayment.STATUS_SUCCEEDED
                )
                payment.status = restored_status
                payment.pre_dispute_status = ""
                payment.save(
                    update_fields=["status", "pre_dispute_status", "updated_at"]
                )
                if payment.donation:
                    payment.donation.payment_status = (
                        StripePaymentService._donation_payment_status_for_stripe_status(
                            restored_status
                        )
                    )
                    payment.donation.save(update_fields=["payment_status"])
                title = "Stripe dispute won"
                message = (
                    f"Dispute closed in our favour on "
                    f"{payment.stripe_payment_intent_id}. Status restored to "
                    f"{restored_status}."
                )
                notif_type = Notification.TYPE_SUCCESS
            elif outcome == "lost":
                payment.status = StripePayment.STATUS_DISPUTE_LOST
                payment.save(update_fields=["status", "updated_at"])
                if payment.donation:
                    payment.donation.payment_status = "dispute_lost"
                    payment.donation.save(update_fields=["payment_status"])
                title = "Stripe dispute lost"
                message = (
                    f"Dispute closed against us on "
                    f"{payment.stripe_payment_intent_id}. "
                    f"Funds withdrawn by Stripe."
                )
                notif_type = Notification.TYPE_ERROR
            else:
                logger.info(
                    "charge.dispute.closed outcome=%s for payment %s — "
                    "no status change",
                    outcome,
                    payment.id,
                )
                return payment

        StripePaymentService._notify_staff_of_payment_event(
            payment=payment,
            title=title,
            message=message,
            notification_type=notif_type,
        )
        logger.info(
            "Processed charge.dispute.closed (%s) for payment %s", outcome, payment.id
        )
        return payment

    @staticmethod
    def log_stripe_response(payment: StripePayment, response: dict[str, Any]) -> None:
        """Log Stripe API response to payment record.

        Args:
            payment: StripePayment instance
            response: Stripe API response dict
        """
        payment.stripe_response = StripePaymentService._persist_intent_snapshot(
            response
        )
        payment.save(update_fields=["stripe_response"])
        logger.debug("Logged Stripe response for payment: %s", payment.id)
