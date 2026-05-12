from typing import Any

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.http import HttpRequest

from payments.models import (
    PaymentGatewayConfig,
    StripeCustomer,
    StripePayment,
    StripePaymentMethod,
    StripeWebhookEvent,
)
from payments.services import validate_stripe_key_prefixes


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):
    """Admin interface for Stripe customers."""

    list_display = [
        "name",
        "email",
        "stripe_customer_id",
        "client",
        "donor",
        "created_at",
    ]
    list_filter = ["created_at"]
    search_fields = ["name", "email", "stripe_customer_id"]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["client", "donor"]


@admin.register(StripePaymentMethod)
class StripePaymentMethodAdmin(admin.ModelAdmin):
    """Admin interface for Stripe payment methods."""

    list_display = [
        "stripe_payment_method_id",
        "stripe_customer",
        "type",
        "card_brand",
        "card_last4",
        "is_default",
        "created_at",
    ]
    list_filter = ["type", "card_brand", "is_default"]
    search_fields = ["stripe_payment_method_id", "stripe_customer__name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(StripePayment)
class StripePaymentAdmin(admin.ModelAdmin):
    """Admin interface for Stripe payments."""

    list_display = [
        "stripe_payment_intent_id",
        "status",
        "amount",
        "currency",
        "created_at",
    ]
    list_filter = ["status", "currency", "created_at"]
    search_fields = ["stripe_payment_intent_id", "stripe_charge_id"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    """Admin interface for Stripe webhook events."""

    list_display = [
        "stripe_event_id",
        "event_type",
        "processed",
        "processing_attempts",
        "created_at",
    ]
    list_filter = ["event_type", "processed", "created_at"]
    search_fields = ["stripe_event_id", "event_type"]
    readonly_fields = [
        "stripe_event_id",
        "event_type",
        "payload",
        "processed",
        "processed_at",
        "processing_attempts",
        "processing_error",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: object) -> bool:
        """Webhook events are created by Stripe, not manually."""
        return False


@admin.register(PaymentGatewayConfig)
class PaymentGatewayConfigAdmin(admin.ModelAdmin):
    """Admin interface for payment gateway configurations.

    The encrypted credential columns are kept editable so an operator can
    rotate keys from the admin. ``save_model`` validates Stripe key prefixes
    so a slot-swap (e.g. pasting a secret key into the publishable slot) is
    rejected with a form error instead of silently breaking Stripe.js.
    """

    list_display = ["client", "provider", "is_active", "created_at"]
    list_filter = ["provider", "is_active"]
    search_fields = ["client__name"]
    autocomplete_fields = ["client"]

    def save_model(
        self,
        request: HttpRequest,
        obj: PaymentGatewayConfig,
        form: Any,
        change: bool,
    ) -> None:
        """Validate Stripe key prefixes before persisting.

        Empty values are allowed so an operator can clear a credential.
        """
        error = validate_stripe_key_prefixes(
            publishable_key=(obj.publishable_key_encrypted or "").strip(),
            secret_key=(obj.secret_key_encrypted or "").strip(),
            webhook_secret=(obj.webhook_secret_encrypted or "").strip(),
        )
        if error:
            raise ValidationError(error)

        super().save_model(request, obj, form, change)
