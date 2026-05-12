"""Comprehensive unit tests for Stripe payment models.

FIN-PAY-UNIT-* test cases covering StripePayment retry/refund logic,
StripeCustomer, StripePaymentMethod, StripeWebhookEvent, and PaymentGatewayConfig.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError

from payments.models import StripePayment
from payments.services import StripePaymentService
from tests.factories import (
    ClientFactory,
    DonorFactory,
    PaymentGatewayConfigFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    StripePaymentMethodFactory,
)

# ═══════════════════════════════════════════════════════════════
# StripePayment — Core Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripePaymentCreation:
    """FIN-PAY-UNIT-001 to 004: StripePayment creation and defaults."""

    def test_create_payment(self) -> None:
        """FIN-PAY-UNIT-001: Payment created with all required fields."""
        payment = StripePaymentFactory()
        assert payment.pk is not None
        assert payment.amount == Decimal("100.00")
        assert payment.currency == "GBP"
        assert payment.status == "pending"

    def test_payment_uuid_pk(self) -> None:
        """FIN-PAY-UNIT-002: Primary key is UUID."""
        payment = StripePaymentFactory()
        assert len(str(payment.pk)) == 36

    def test_payment_status_constants(self) -> None:
        """FIN-PAY-UNIT-003: Status constants have exact values."""
        assert StripePayment.STATUS_PENDING == "pending"
        assert StripePayment.STATUS_PROCESSING == "processing"
        assert StripePayment.STATUS_SUCCEEDED == "succeeded"
        assert StripePayment.STATUS_FAILED == "failed"
        assert StripePayment.STATUS_REFUNDED == "refunded"
        assert StripePayment.STATUS_PARTIALLY_REFUNDED == "partially_refunded"
        assert StripePayment.STATUS_CANCELLED == "cancelled"

    def test_payment_intent_id_unique(self) -> None:
        """FIN-PAY-UNIT-004: stripe_payment_intent_id must be unique."""
        StripePaymentFactory(stripe_payment_intent_id="pi_duplicate_test")
        with pytest.raises(IntegrityError):
            StripePaymentFactory(stripe_payment_intent_id="pi_duplicate_test")


# ═══════════════════════════════════════════════════════════════
# StripePayment — Retry Logic
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripePaymentRetry:
    """FIN-PAY-UNIT-005 to 008: can_retry method."""

    def test_can_retry_failed_with_retries_remaining(self) -> None:
        """FIN-PAY-UNIT-005: can_retry True for failed + retries remaining."""
        payment = StripePaymentFactory(status="failed", retry_count=1, max_retries=3)
        assert StripePaymentService.can_retry(payment) is True

    def test_cannot_retry_failed_max_reached(self) -> None:
        """FIN-PAY-UNIT-006: can_retry False when max retries reached."""
        payment = StripePaymentFactory(status="failed", retry_count=3, max_retries=3)
        assert StripePaymentService.can_retry(payment) is False

    def test_cannot_retry_succeeded(self) -> None:
        """FIN-PAY-UNIT-007: can_retry False for succeeded payments."""
        payment = StripePaymentFactory(status="succeeded")
        assert StripePaymentService.can_retry(payment) is False

    def test_cannot_retry_pending(self) -> None:
        """FIN-PAY-UNIT-008: can_retry False for pending payments."""
        payment = StripePaymentFactory(status="pending")
        assert StripePaymentService.can_retry(payment) is False


# ═══════════════════════════════════════════════════════════════
# StripePayment — Refund Logic
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripePaymentRefund:
    """FIN-PAY-UNIT-009 to 013: can_refund method."""

    def test_can_refund_succeeded_not_refunded(self) -> None:
        """FIN-PAY-UNIT-009: can_refund True for succeeded, no refunds."""
        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("0"),
        )
        assert StripePaymentService.can_refund(payment) is True

    def test_can_refund_partially_refunded(self) -> None:
        """FIN-PAY-UNIT-010: can_refund True if partial refund done."""
        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("50.00"),
        )
        assert StripePaymentService.can_refund(payment) is True

    def test_cannot_refund_fully_refunded(self) -> None:
        """FIN-PAY-UNIT-011: can_refund False if fully refunded."""
        payment = StripePaymentFactory(
            status="succeeded",
            amount=Decimal("100.00"),
            amount_refunded=Decimal("100.00"),
        )
        assert StripePaymentService.can_refund(payment) is False

    def test_cannot_refund_failed(self) -> None:
        """FIN-PAY-UNIT-012: can_refund False for failed payments."""
        payment = StripePaymentFactory(status="failed", amount=Decimal("100.00"))
        assert StripePaymentService.can_refund(payment) is False

    def test_payment_str_format(self) -> None:
        """FIN-PAY-UNIT-013: __str__ includes currency, amount, status."""
        payment = StripePaymentFactory(
            stripe_payment_intent_id="pi_strtest001",
            amount=Decimal("250.00"),
            currency="GBP",
            status="succeeded",
        )
        s = str(payment)
        assert "GBP" in s
        assert "250.00" in s
        assert "succeeded" in s


# ═══════════════════════════════════════════════════════════════
# StripePayment — Error Tracking
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripePaymentErrors:
    """FIN-PAY-UNIT-014 to 015: Error message and code storage."""

    def test_error_fields_stored(self) -> None:
        """FIN-PAY-UNIT-014: Error message, code, type stored."""
        payment = StripePaymentFactory(status="failed")
        payment.error_message = "Your card was declined."
        payment.error_code = "card_declined"
        payment.error_type = "card_error"
        payment.save()
        payment.refresh_from_db()
        assert payment.error_message == "Your card was declined."
        assert payment.error_code == "card_declined"

    def test_stripe_response_json(self) -> None:
        """FIN-PAY-UNIT-015: stripe_response stores JSON correctly."""
        payment = StripePaymentFactory()
        payment.stripe_response = {"id": "pi_test", "status": "succeeded"}
        payment.save()
        payment.refresh_from_db()
        assert payment.stripe_response["status"] == "succeeded"


# ═══════════════════════════════════════════════════════════════
# StripeCustomer — Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripeCustomer:
    """FIN-PAY-UNIT-016 to 019: StripeCustomer model."""

    def test_create_customer(self) -> None:
        """FIN-PAY-UNIT-016: Customer created with required fields."""
        customer = StripeCustomerFactory()
        assert customer.pk is not None
        assert customer.stripe_customer_id.startswith("cus_")

    def test_customer_id_unique(self) -> None:
        """FIN-PAY-UNIT-017: stripe_customer_id must be unique."""
        StripeCustomerFactory(stripe_customer_id="cus_dup_test")
        with pytest.raises(IntegrityError):
            StripeCustomerFactory(stripe_customer_id="cus_dup_test")

    def test_customer_linked_to_client(self) -> None:
        """FIN-PAY-UNIT-018: Customer linked to Client."""
        client = ClientFactory(name="Customer Client")
        customer = StripeCustomerFactory(client=client)
        assert customer.client.name == "Customer Client"

    def test_customer_linked_to_donor(self) -> None:
        """FIN-PAY-UNIT-019: Customer linked to Donor."""
        donor = DonorFactory(first_name="Jane")
        customer = StripeCustomerFactory(donor=donor, client=None)
        assert customer.donor.first_name == "Jane"


# ═══════════════════════════════════════════════════════════════
# StripePaymentMethod — Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestStripePaymentMethod:
    """FIN-PAY-UNIT-020 to 022: StripePaymentMethod model."""

    def test_create_payment_method(self) -> None:
        """FIN-PAY-UNIT-020: Payment method created with card details."""
        pm = StripePaymentMethodFactory()
        assert pm.pk is not None
        assert pm.card_brand == "visa"
        assert pm.card_last4 == "4242"

    def test_payment_method_str_card(self) -> None:
        """FIN-PAY-UNIT-021: __str__ shows 'Visa ****4242'."""
        pm = StripePaymentMethodFactory(card_brand="visa", card_last4="4242")
        assert str(pm) == "Visa ****4242"

    def test_payment_method_str_no_card(self) -> None:
        """FIN-PAY-UNIT-022: __str__ fallback without card info."""
        pm = StripePaymentMethodFactory(
            card_brand="",
            card_last4="",
            stripe_payment_method_id="pm_nocardtest",
        )
        assert "pm_nocardtest" in str(pm)


# ═══════════════════════════════════════════════════════════════
# PaymentGatewayConfig — Tests
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestPaymentGatewayConfig:
    """FIN-PAY-UNIT-023 to 025: PaymentGatewayConfig model."""

    def test_create_config(self) -> None:
        """FIN-PAY-UNIT-023: Config created for client+provider."""
        config = PaymentGatewayConfigFactory()
        assert config.pk is not None
        assert config.provider == "stripe"
        assert config.is_active is True

    def test_unique_client_provider_constraint(self) -> None:
        """FIN-PAY-UNIT-024: Same client+provider combination rejected."""
        client = ClientFactory()
        PaymentGatewayConfigFactory(client=client, provider="stripe")
        with pytest.raises(IntegrityError):
            PaymentGatewayConfigFactory(client=client, provider="stripe")

    def test_different_providers_same_client(self) -> None:
        """FIN-PAY-UNIT-025: Different providers for same client allowed."""
        client = ClientFactory()
        PaymentGatewayConfigFactory(client=client, provider="stripe")
        config2 = PaymentGatewayConfigFactory(client=client, provider="sagepay")
        assert config2.pk is not None
