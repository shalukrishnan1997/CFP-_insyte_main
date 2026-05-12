"""Stripe payment models for the donation management system."""

import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, cast

from django.db import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    connections,
    models,
    transaction,
)
from django.db import connection as default_connection
from encrypted_fields import fields as encrypted_fields

from core.models.base import CreateAndUpdateTimestampModel

if TYPE_CHECKING:
    from donations.models import Donation

logger = logging.getLogger(__name__)


class StripeCustomer(CreateAndUpdateTimestampModel):
    """Stripe customer model for storing customer information.

    Links Stripe customers to either Client (charities) or Donor entities.

    Attributes:
        id: UUID primary key.
        stripe_customer_id: Stripe customer ID (starts with cus_).
        client: Optional foreign key to Client.
        donor: Optional foreign key to Donor.
        email: Customer email address.
        name: Customer full name.
        phone: Customer phone number.
        metadata: Additional Stripe customer metadata.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_customer_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Stripe customer ID (starts with cus_)",
    )

    # Link to either Client or Donor
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_customers",
        help_text="Linked charity/client organization",
    )
    donor = models.ForeignKey(
        "donors.Donor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_customers",
        help_text="Linked donor for donation payments",
    )

    # Customer details
    email = models.EmailField(help_text="Customer email address")
    name = models.CharField(max_length=255, help_text="Customer full name")
    phone = models.CharField(
        max_length=50, blank=True, default="", help_text="Customer phone number"
    )

    # Metadata
    metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional Stripe customer metadata"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stripe_customer_id"]),
            models.Index(fields=["email"]),
        ]
        verbose_name = "Stripe Customer"
        verbose_name_plural = "Stripe Customers"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Customer name and Stripe ID.
        """
        return f"{self.name} ({self.stripe_customer_id})"


class StripePaymentMethod(CreateAndUpdateTimestampModel):
    """Stripe payment method (saved cards) for customers.

    Attributes:
        id: UUID primary key.
        stripe_payment_method_id: Stripe payment method ID (starts with pm_).
        stripe_customer: Associated Stripe customer.
        type: Payment method type (card, etc.).
        card_brand: Card brand (visa, mastercard, etc.).
        card_last4: Last 4 digits of card.
        card_exp_month: Card expiration month.
        card_exp_year: Card expiration year.
        is_default: Default payment method for customer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_payment_method_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Stripe payment method ID (starts with pm_)",
    )
    stripe_customer = models.ForeignKey(
        StripeCustomer,
        on_delete=models.CASCADE,
        related_name="payment_methods",
        help_text="Associated Stripe customer",
    )

    # Payment method details
    type = models.CharField(
        max_length=50, default="card", help_text="Payment method type (card, etc.)"
    )
    card_brand = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Card brand (visa, mastercard, etc.)",
    )
    card_last4 = models.CharField(
        max_length=4, blank=True, default="", help_text="Last 4 digits of card"
    )
    card_exp_month = models.IntegerField(
        null=True, blank=True, help_text="Card expiration month"
    )
    card_exp_year = models.IntegerField(
        null=True, blank=True, help_text="Card expiration year"
    )
    is_default = models.BooleanField(
        default=False, help_text="Default payment method for customer"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stripe_payment_method_id"]),
            models.Index(fields=["stripe_customer", "is_default"]),
        ]
        verbose_name = "Stripe Payment Method"
        verbose_name_plural = "Stripe Payment Methods"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Card brand and last 4 digits, or payment method ID.
        """
        if self.card_brand and self.card_last4:
            return f"{self.card_brand.title()} ****{self.card_last4}"
        return f"Payment Method {self.stripe_payment_method_id}"


class StripePayment(CreateAndUpdateTimestampModel):
    """Stripe payment record with full audit trail and retry logic.

    Attributes:
        id: UUID primary key.
        stripe_payment_intent_id: Stripe payment intent ID (starts with pi_).
        stripe_charge_id: Stripe charge ID (starts with ch_).
        stripe_checkout_session_id: Stripe checkout session ID (starts with cs_).
        stripe_customer: Customer who made the payment.
        stripe_payment_method: Payment method used.
        invoice: Invoice being paid.
        donation: Donation payment.
        amount: Payment amount.
        currency: Payment currency.
        status: Payment status.
        amount_refunded: Total amount refunded.
        refund_reason: Reason for refund.
        retry_count: Number of retry attempts.
        max_retries: Maximum retry attempts.
        next_retry_at: Scheduled next retry time.
        last_retry_at: Last retry attempt time.
        stripe_response: Full Stripe API response.
        error_message: Error message if payment failed.
        error_code: Stripe error code.
        error_type: Stripe error type.
        description: Payment description.
        metadata: Additional payment metadata.
        processed_by: User who initiated payment.
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_REQUIRES_CAPTURE = "requires_capture"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_REFUNDED = "refunded"
    STATUS_PARTIALLY_REFUNDED = "partially_refunded"
    STATUS_CANCELLED = "cancelled"
    STATUS_DISPUTED = "disputed"
    STATUS_DISPUTE_LOST = "dispute_lost"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_REQUIRES_CAPTURE, "Requires Capture (MOTO auth)"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REFUNDED, "Refunded"),
        (STATUS_PARTIALLY_REFUNDED, "Partially Refunded"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_DISPUTED, "Disputed"),
        (STATUS_DISPUTE_LOST, "Dispute Lost"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Stripe IDs
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Stripe payment intent ID (starts with pi_)",
    )
    stripe_charge_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe charge ID (starts with ch_)",
    )
    stripe_checkout_session_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe checkout session ID (starts with cs_)",
    )
    stripe_customer = models.ForeignKey(
        StripeCustomer,
        on_delete=models.PROTECT,
        related_name="payments",
        help_text="Customer who made the payment",
    )
    stripe_payment_method = models.ForeignKey(
        StripePaymentMethod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        help_text="Payment method used",
    )

    # Link to Invoice or Donation
    invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_payments",
        help_text="Invoice being paid",
    )
    donation = models.ForeignKey(
        "donations.Donation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_payments",
        help_text="Donation payment",
    )

    # Payment details
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="Payment amount"
    )
    currency = models.CharField(
        max_length=3, default="GBP", help_text="Payment currency (GBP, USD, EUR)"
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Payment status",
    )

    # Refund tracking
    amount_refunded = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Total amount refunded"
    )
    refund_reason = models.TextField(
        blank=True, default="", help_text="Reason for refund"
    )

    # Dispute / chargeback tracking — pre-dispute status snapshot lets a
    # successful chargeback rebuttal restore the prior payment state.
    pre_dispute_status = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="Status snapshot taken when dispute opened, restored if dispute is won",
    )
    dispute_reason = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Stripe-reported dispute reason",
    )

    # 3DS / SCA fallback. When a PaymentIntent comes back ``requires_action``
    # we persist the Stripe-hosted next-action URL so QA can dispatch a
    # Checkout authentication link to the donor. The PaymentIntent
    # ``client_secret`` is a bearer token and is deliberately *not* persisted —
    # storing it would leak via AuditLog and Sentry; the QA Checkout fallback
    # flow only needs ``requires_action_url``.
    requires_action_url = models.URLField(
        max_length=2048,
        blank=True,
        default="",
        help_text=(
            "Stripe-hosted next_action.use_stripe_sdk URL captured when SCA "
            "action is required. Replaced when a Checkout fallback link is "
            "issued to the donor."
        ),
    )
    authentication_link_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the donor was emailed a Stripe Checkout link to complete "
            "3DS / SCA authentication for this payment."
        ),
    )

    # Retry tracking
    retry_count = models.IntegerField(default=0, help_text="Number of retry attempts")
    max_retries = models.IntegerField(default=3, help_text="Maximum retry attempts")
    next_retry_at = models.DateTimeField(
        null=True, blank=True, help_text="Scheduled next retry time"
    )
    last_retry_at = models.DateTimeField(
        null=True, blank=True, help_text="Last retry attempt time"
    )

    # Response logging
    stripe_response = models.JSONField(
        default=dict, blank=True, help_text="Full Stripe API response"
    )
    error_message = models.TextField(
        blank=True, default="", help_text="Error message if payment failed"
    )
    error_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe error code",
    )
    error_type = models.CharField(
        max_length=100, blank=True, default="", help_text="Stripe error type"
    )

    # Metadata
    description = models.TextField(
        blank=True, default="", help_text="Payment description"
    )
    metadata = models.JSONField(
        default=dict, blank=True, help_text="Additional payment metadata"
    )

    # Audit trail
    processed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_payments",
        help_text="User who initiated payment",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stripe_payment_intent_id"]),
            models.Index(fields=["stripe_charge_id"]),
            models.Index(fields=["stripe_checkout_session_id"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["invoice", "status"]),
            models.Index(fields=["donation", "status"]),
            models.Index(fields=["next_retry_at"]),
        ]
        verbose_name = "Stripe Payment"
        verbose_name_plural = "Stripe Payments"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Currency, amount, status, and payment intent ID.
        """
        return f"{self.currency} {self.amount} - {self.status} ({self.stripe_payment_intent_id})"


class StripeWebhookEvent(CreateAndUpdateTimestampModel):
    """Stripe webhook event log for idempotent processing.

    Attributes:
        id: UUID primary key.
        stripe_event_id: Stripe event ID (starts with evt_).
        event_type: Stripe event type (e.g., payment_intent.succeeded).
        payload: Full webhook event payload.
        processed: Whether event has been processed.
        processed_at: When event was processed.
        processing_attempts: Number of processing attempts.
        processing_error: Error message if processing failed.
        payment: Related payment record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Stripe event ID (starts with evt_)",
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Stripe event type (e.g., payment_intent.succeeded)",
    )

    # Event payload
    payload = models.JSONField(help_text="Full webhook event payload")

    # Processing status
    processed = models.BooleanField(
        default=False, db_index=True, help_text="Whether event has been processed"
    )
    processed_at = models.DateTimeField(
        null=True, blank=True, help_text="When event was processed"
    )
    processing_attempts = models.IntegerField(
        default=0, help_text="Number of processing attempts"
    )
    processing_error = models.TextField(
        blank=True, default="", help_text="Error message if processing failed"
    )

    # Related payment
    payment = models.ForeignKey(
        StripePayment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        help_text="Related payment record",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stripe_event_id"]),
            models.Index(fields=["event_type", "-created_at"]),
            models.Index(fields=["processed", "-created_at"]),
        ]
        verbose_name = "Stripe Webhook Event"
        verbose_name_plural = "Stripe Webhook Events"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Event type, processing status, and event ID.
        """
        status = "Processed" if self.processed else "Pending"
        return f"{self.event_type} - {status} ({self.stripe_event_id})"


class PaymentGatewayConfig(CreateAndUpdateTimestampModel):
    """Configuration for payment gateways per client.

    Sensitive credentials (Stripe ``secret_key``, ``webhook_secret``,
    ``publishable_key``) are stored in dedicated ``EncryptedCharField``
    columns.

    Attributes:
        id: UUID primary key.
        client: Foreign key to Client.
        provider: Payment provider type (stripe, sagepay, worldpay).
        is_active: Whether this configuration is active.
        secret_key_encrypted: Stripe secret API key (sk_live_… / sk_test_…).
        webhook_secret_encrypted: Stripe webhook signing secret.
        publishable_key_encrypted: Stripe publishable key.
    """

    # The audit signal scrapes every concrete field's value into AuditLog.changes;
    # the encrypted-column ORM round-trip surfaces decrypted plaintext, so list
    # the column attnames here to keep them out of the audit metadata.
    audit_exclude_fields = (
        "secret_key_encrypted",
        "webhook_secret_encrypted",
        "publishable_key_encrypted",
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="payment_configs",
        help_text="Client associated with this configuration",
    )
    provider = models.CharField(
        max_length=20,
        choices=[
            ("stripe", "Stripe"),
            ("sagepay", "SagePay"),
            ("worldpay", "WorldPay"),
        ],
        help_text="Payment provider type",
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this configuration is active"
    )
    # ``null=True`` is required because EncryptedCharField.get_prep_value("")
    # returns ``None`` — the encryption layer writes NULL for empty strings,
    # mirroring the pattern on ``Donation.sort_code`` / ``Donation.account_number``.
    secret_key_encrypted = encrypted_fields.EncryptedCharField(
        max_length=255,
        blank=True,
        null=True,
        default="",
        help_text="Encrypted Stripe secret API key",
    )
    webhook_secret_encrypted = encrypted_fields.EncryptedCharField(
        max_length=255,
        blank=True,
        null=True,
        default="",
        help_text="Encrypted Stripe webhook signing secret",
    )
    publishable_key_encrypted = encrypted_fields.EncryptedCharField(
        max_length=255,
        blank=True,
        null=True,
        default="",
        help_text="Encrypted Stripe publishable key",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "provider"],
                name="unique_payment_gateway_client_provider",
            ),
        ]
        verbose_name = "Payment Gateway Config"
        verbose_name_plural = "Payment Gateway Configs"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Client name and provider display.
        """
        return f"{self.client.name} - {self.get_provider_display()}"

    def get_secret_key(self) -> str:
        """Return the Stripe secret key."""
        return str(self.secret_key_encrypted or "")

    def get_webhook_secret(self) -> str:
        """Return the Stripe webhook signing secret."""
        return str(self.webhook_secret_encrypted or "")

    def get_publishable_key(self) -> str:
        """Return the Stripe publishable key."""
        return str(self.publishable_key_encrypted or "")


class StripePaymentAttemptManager(models.Manager["StripePaymentAttempt"]):
    """Records Stripe attempts on a connection that survives the caller's rollback."""

    # Cap retries on attempt-number collisions to avoid infinite loops if the
    # underlying constraint keeps firing (e.g. clock skew, repeated concurrent
    # writers). Two retries comfortably absorbs the realistic concurrency
    # window for a single donation (two QA approvers racing).
    _MAX_INTEGRITY_RETRIES = 2

    def log_attempt(
        self,
        *,
        donation: Donation,
        amount_cents: int,
        currency: str,
        status: str,
        attempt_number: int,
        stripe_payment_intent_id: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> StripePaymentAttempt | None:
        """Persist an attempt so the audit row survives the caller's rollback.

        On PostgreSQL (production) the INSERT runs on a freshly opened
        autocommit connection, so it is committed independently of any wrapping
        ``transaction.atomic()`` that may later call ``set_rollback(True)``.

        On SQLite (tests / local dev) the database is single-writer, so the
        write must use the caller's connection and will share its commit/
        rollback fate. The rollback-survival contract is exercised in
        production only.

        Concurrency: ``next_attempt_number`` is a read-then-insert, so two
        concurrent approvals for the same donation can compute the same
        ``attempt_number``. The DB-level ``UniqueConstraint(donation,
        attempt_number)`` catches the race; on ``IntegrityError`` we recompute
        the next number and retry, capped at ``_MAX_INTEGRITY_RETRIES``.

        Error policy: infrastructure failures (``OperationalError``,
        ``InterfaceError``, generic ``DatabaseError``) are re-raised — the
        write almost certainly did not happen and silently dropping it would
        defeat the durable-audit purpose of this log. Logical errors
        (``ValueError``, ``ValidationError``, etc.) are still swallowed and
        logged so a malformed audit row does not poison the payment flow.

        Args:
            donation: Donation the attempt relates to.
            amount_cents: Amount in minor units.
            currency: 3-letter ISO currency code.
            status: One of the ``STATUS_*`` choices on ``StripePaymentAttempt``.
            attempt_number: Monotonic 1-based attempt counter for this donation.
            stripe_payment_intent_id: Stripe PaymentIntent ID, if assigned.
            error_code: Stripe error code, if any.
            error_message: Stripe error message, if any.

        Returns:
            StripePaymentAttempt | None: The persisted row, or ``None`` if the
            write failed with a logical (non-infra) error.

        Raises:
            OperationalError: DB connection / transport failure.
            InterfaceError: DB driver-level failure.
            DatabaseError: Other database-level failures that mean the row was
                not written.
        """
        current_attempt_number = attempt_number

        for retry in range(self._MAX_INTEGRITY_RETRIES + 1):
            fields: dict[str, object] = {
                "donation": donation,
                "stripe_payment_intent_id": stripe_payment_intent_id,
                "status": status,
                "error_code": error_code,
                "error_message": error_message,
                "amount_cents": amount_cents,
                "currency": currency,
                "attempt_number": current_attempt_number,
            }

            try:
                if default_connection.vendor == "sqlite":
                    # Wrap in a savepoint so an IntegrityError on the unique
                    # constraint does not poison the surrounding atomic block
                    # (typical in tests and in QA-approval views that wrap
                    # calls in transaction.atomic()).
                    with transaction.atomic():
                        return self.create(**fields)
                return self._insert_via_fresh_connection(fields)
            except IntegrityError as exc:
                # Collision on (donation, attempt_number). Recompute and retry.
                if retry >= self._MAX_INTEGRITY_RETRIES:
                    logger.error(
                        "Exhausted retries logging StripePaymentAttempt for "
                        "donation %s after %d collisions: %s",
                        donation.pk,
                        retry + 1,
                        exc,
                    )
                    raise
                next_number = self.next_attempt_number(donation)
                logger.warning(
                    "StripePaymentAttempt collision on donation %s at "
                    "attempt_number=%d; retrying with %d (retry %d/%d)",
                    donation.pk,
                    current_attempt_number,
                    next_number,
                    retry + 1,
                    self._MAX_INTEGRITY_RETRIES,
                )
                current_attempt_number = next_number
                continue
            except (OperationalError, InterfaceError, DatabaseError) as exc:
                # Infra failure — the row was almost certainly not written.
                # Re-raise so the caller sees the durability gap rather than
                # silently losing the audit entry.
                logger.error(
                    "Database error logging StripePaymentAttempt for donation %s: %s",
                    donation.pk,
                    exc,
                )
                raise
            except Exception as exc:
                # Logical error (ValueError, ValidationError, etc.) — log but
                # do not raise. We never want a malformed audit row to break
                # the surrounding payment flow.
                logger.error(
                    "Failed to log StripePaymentAttempt for donation %s: %s",
                    donation.pk,
                    exc,
                )
                return None

        # Defensive: should be unreachable because the loop returns or raises.
        return None

    def _insert_via_fresh_connection(
        self, fields: dict[str, object]
    ) -> StripePaymentAttempt:
        """Insert the attempt row through a fresh autocommit connection.

        Production (PostgreSQL) verification: to assert rollback survival,
        wrap a ``log_attempt`` call in ``transaction.atomic()``, force a
        rollback after it returns, then read the row back through a fresh
        connection (e.g. ``connections.create_connection('default')``). The
        row must persist. This is not feasible to assert under the project's
        SQLite test settings — SQLite serializes writers at the database
        level, so a second connection cannot complete a write while the
        first holds the write transaction. Verify on a Postgres staging
        environment instead.
        """
        connection = connections.create_connection("default")
        connection.set_autocommit(True)
        attempt_id = uuid.uuid4()
        donation = fields["donation"]
        donation_pk = getattr(donation, "pk", donation)
        params: list[str | int] = [
            str(attempt_id),
            str(donation_pk),
            cast("str", fields["stripe_payment_intent_id"]),
            cast("str", fields["status"]),
            cast("str", fields["error_code"]),
            cast("str", fields["error_message"]),
            cast("int", fields["amount_cents"]),
            cast("str", fields["currency"]),
            cast("int", fields["attempt_number"]),
        ]
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO payments_stripepaymentattempt (
                        id, created_at, updated_at, donation_id,
                        stripe_payment_intent_id, status, error_code,
                        error_message, amount_cents, currency, attempt_number
                    ) VALUES (
                        %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    params,
                )
        finally:
            with contextlib.suppress(Exception):
                connection.close()
        return self.model(id=attempt_id, **fields)

    def next_attempt_number(self, donation: Donation) -> int:
        """Return the next 1-based attempt number for a donation."""
        last = (
            self.filter(donation=donation)
            .order_by("-attempt_number")
            .values_list("attempt_number", flat=True)
            .first()
        )
        return (last or 0) + 1


class StripePaymentAttempt(CreateAndUpdateTimestampModel):
    """Durable log of every Stripe attempt for a donation, including failures.

    Rows are written through ``StripePaymentAttempt.objects.log_attempt`` on a
    fresh autocommit connection so the audit trail survives an outer
    ``transaction.set_rollback(True)`` triggered by the QA approval flow.

    Attributes:
        id: UUID primary key.
        donation: Donation the attempt was made for.
        stripe_payment_intent_id: Stripe PaymentIntent ID (may be blank if the API call never returned one).
        status: Outcome of the attempt.
        error_code: Stripe error code, if any.
        error_message: Stripe error message, if any.
        amount_cents: Amount in minor units (e.g. pence).
        currency: 3-letter ISO currency code.
        attempt_number: Monotonic 1-based counter per donation.
    """

    STATUS_PENDING = "pending"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_REQUIRES_ACTION = "requires_action"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REQUIRES_ACTION, "Requires Action"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    donation = models.ForeignKey(
        "donations.Donation",
        on_delete=models.CASCADE,
        related_name="stripe_attempts",
        help_text="Donation the attempt was made for",
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Stripe PaymentIntent ID (blank if Stripe returned no intent)",
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        db_index=True,
        help_text="Outcome of the attempt",
    )
    error_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Stripe error code, if any",
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Stripe error message, if any",
    )
    amount_cents = models.PositiveIntegerField(
        help_text="Amount in minor units (e.g. pence)"
    )
    currency = models.CharField(
        max_length=3,
        default="GBP",
        help_text="3-letter ISO currency code",
    )
    attempt_number = models.PositiveSmallIntegerField(
        help_text="1-based attempt counter per donation"
    )

    objects: StripePaymentAttemptManager = StripePaymentAttemptManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["donation", "attempt_number"]),
            models.Index(fields=["status", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["donation", "attempt_number"],
                name="uniq_attempt_per_donation",
            ),
        ]
        verbose_name = "Stripe Payment Attempt"
        verbose_name_plural = "Stripe Payment Attempts"

    def __str__(self) -> str:
        """Return string representation."""
        return (
            f"Attempt #{self.attempt_number} for donation {self.donation_id} "
            f"({self.status})"
        )
