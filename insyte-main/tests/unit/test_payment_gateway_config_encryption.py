"""Tests for the encrypted-credential storage on PaymentGatewayConfig.

Stripe ``secret_key`` / ``webhook_secret`` / ``publishable_key`` live in
dedicated ``EncryptedCharField`` columns. The accessors return the
plaintext from those columns; nothing else.
"""

from __future__ import annotations

import pytest
from django.db import connection

from payments.models import PaymentGatewayConfig
from tests.factories import (
    ClientFactory,
    PaymentGatewayConfigFactory,
)


@pytest.mark.django_db()
class TestPaymentGatewayConfigCredentials:
    def test_get_methods_read_encrypted_columns(self) -> None:
        """``get_*`` returns the plaintext from the encrypted columns."""
        cfg = PaymentGatewayConfigFactory(
            secret_key_encrypted="sk_test_NEW",
            webhook_secret_encrypted="whsec_test_NEW",
            publishable_key_encrypted="pk_test_NEW",
        )
        cfg.refresh_from_db()
        assert cfg.get_secret_key() == "sk_test_NEW"
        assert cfg.get_webhook_secret() == "whsec_test_NEW"
        assert cfg.get_publishable_key() == "pk_test_NEW"

    def test_get_methods_return_empty_when_unset(self) -> None:
        """An empty/NULL encrypted column returns an empty string."""
        cfg = PaymentGatewayConfig.objects.create(
            client=ClientFactory(),
            provider="stripe",
            is_active=True,
        )
        assert cfg.get_secret_key() == ""
        assert cfg.get_webhook_secret() == ""
        assert cfg.get_publishable_key() == ""

    def test_database_row_does_not_contain_plaintext_secret(self) -> None:
        """The encrypted column must hold ciphertext, not the secret value."""
        client = ClientFactory()
        cfg = PaymentGatewayConfig.objects.create(
            client=client,
            provider="stripe",
            is_active=True,
        )
        cfg.secret_key_encrypted = "sk_test_LEAKABLE"
        cfg.save(update_fields=["secret_key_encrypted"])

        # Read the raw column bypassing the EncryptedCharField descriptor.
        # Match by (client, provider) which has a unique constraint — avoids
        # the SQLite vs Postgres UUID-string-format mismatch.
        with connection.cursor() as cur:
            cur.execute(
                "SELECT secret_key_encrypted FROM payments_paymentgatewayconfig "
                "WHERE provider = %s",
                ["stripe"],
            )
            row = cur.fetchone()
        assert row is not None, "row not found in raw query"
        (raw,) = row
        assert "sk_test_LEAKABLE" not in (raw or ""), (
            f"Encrypted column leaked plaintext to the database: {raw!r}"
        )

    def test_audit_exclude_fields_lists_all_encrypted_columns(self) -> None:
        """``audit/signals.py:get_instance_changes`` reads this attribute."""
        excluded = set(PaymentGatewayConfig.audit_exclude_fields)
        assert excluded == {
            "secret_key_encrypted",
            "webhook_secret_encrypted",
            "publishable_key_encrypted",
        }

    def test_update_does_not_leak_encrypted_plaintext_to_audit_log(self) -> None:
        """Encrypted columns must be redacted from ``AuditLog.changes`` even
        though the ORM hands the signal the decrypted plaintext."""
        from audit.models import AuditLog

        cfg = PaymentGatewayConfig.objects.create(
            client=ClientFactory(),
            provider="stripe",
            is_active=True,
        )
        cfg.secret_key_encrypted = "sk_test_DO_NOT_LOG"
        cfg.webhook_secret_encrypted = "whsec_DO_NOT_LOG"
        cfg.publishable_key_encrypted = "pk_test_DO_NOT_LOG"
        cfg.save()

        # CREATE row should have changes={} (no diff). Trigger an UPDATE.
        cfg.secret_key_encrypted = "sk_test_ROTATED"
        cfg.save()

        update_log = (
            AuditLog.objects.filter(model_name="PaymentGatewayConfig", action="UPDATE")
            .order_by("-created_at")
            .first()
        )
        assert update_log is not None, "expected an UPDATE AuditLog row"

        changes = update_log.changes
        # Encrypted column names must not appear at all in the audit diff.
        assert "secret_key_encrypted" not in changes
        assert "webhook_secret_encrypted" not in changes
        assert "publishable_key_encrypted" not in changes

        # And no plaintext credential value can leak via str() of the dict.
        flat = str(changes)
        for forbidden in (
            "sk_test_DO_NOT_LOG",
            "sk_test_ROTATED",
            "whsec_DO_NOT_LOG",
            "pk_test_DO_NOT_LOG",
        ):
            assert forbidden not in flat, (
                f"plaintext credential {forbidden!r} leaked into AuditLog.changes"
            )


@pytest.mark.django_db()
class TestStripeWebhookSecretLookup:
    """``core/webhooks.py:_get_stripe_webhook_secrets`` reads the encrypted column."""

    def test_secrets_are_collected_from_encrypted_column(self) -> None:
        from core.webhooks import _get_stripe_webhook_secrets

        cfg = PaymentGatewayConfig.objects.create(
            client=ClientFactory(),
            provider="stripe",
            is_active=True,
        )
        cfg.webhook_secret_encrypted = "whsec_FROM_ENCRYPTED"
        cfg.save(update_fields=["webhook_secret_encrypted"])

        secrets = _get_stripe_webhook_secrets()
        assert "whsec_FROM_ENCRYPTED" in secrets

    def test_inactive_configs_are_skipped(self) -> None:
        from core.webhooks import _get_stripe_webhook_secrets

        cfg = PaymentGatewayConfig.objects.create(
            client=ClientFactory(),
            provider="stripe",
            is_active=False,
        )
        cfg.webhook_secret_encrypted = "whsec_INACTIVE"
        cfg.save(update_fields=["webhook_secret_encrypted"])

        assert "whsec_INACTIVE" not in _get_stripe_webhook_secrets()
