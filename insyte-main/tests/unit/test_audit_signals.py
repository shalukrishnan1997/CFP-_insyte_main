"""Tests for ``audit.signals.audit_post_save`` failure handling.

The audit signal historically swallowed every exception from
``AuditLog.objects.create`` with a single ``logger.error`` and let the parent
save complete. For financial / state-changing rows that means a Donation or
StripePayment could land in the database with no audit trail — a compliance
gap.

These tests pin the new contract:

* For a model in ``CRITICAL_AUDITED_MODELS`` the exception is captured to
  Sentry **and re-raised**, rolling back the parent transaction.
* For other audited models the exception is captured to Sentry but
  swallowed, so the parent save still succeeds.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from audit.models import AuditLog
from audit.signals import CRITICAL_AUDITED_MODELS
from donations.models import Donation
from invoices.models import Invoice
from tests.factories import CampaignFactory, DonationFactory, UserFactory


class TestCriticalAuditedModelsConstant:
    """Sanity-check the constant the failure path keys off."""

    def test_critical_set_contains_financial_models(self) -> None:
        assert {
            "Donation",
            "DonationBatch",
            "Invoice",
            "PayingInSlip",
            "PaymentGatewayConfig",
            "StripePayment",
        } == CRITICAL_AUDITED_MODELS


@pytest.mark.django_db()
class TestAuditPostSaveFailureCriticalModels:
    """Critical-model audit failures must surface to the caller."""

    def test_donation_save_re_raises_when_audit_log_create_fails(self) -> None:
        """An audit DB outage on a Donation re-raises to the caller."""
        # Build the Donation (and its supporting rows) outside the patched
        # window so unrelated AuditLog inserts succeed normally. We then
        # simulate an audit-table outage on the *next* save.
        donation = DonationFactory(amount=Decimal("42.00"))

        boom = RuntimeError("audit table locked")
        with (
            patch(
                "audit.models.AuditLog.objects.create",
                side_effect=boom,
            ),
            patch("audit.signals.sentry_sdk.capture_exception") as captured,
            pytest.raises(RuntimeError, match="audit table locked"),
        ):
            donation.amount = Decimal("99.00")
            donation.save(update_fields=["amount", "updated_at"])

        captured.assert_called_once()
        # Sentry receives the original exception instance.
        assert captured.call_args.args[0] is boom

    def test_critical_save_failure_rolls_back_parent_row(self) -> None:
        """Donation update must not persist when audit insert fails inside a txn."""
        from django.db import transaction

        donation = DonationFactory(amount=Decimal("10.00"))
        original_amount = donation.amount

        with (
            patch(
                "audit.models.AuditLog.objects.create",
                side_effect=RuntimeError("audit table locked"),
            ),
            patch("audit.signals.sentry_sdk.capture_exception"),
            pytest.raises(RuntimeError),
            transaction.atomic(),
        ):
            donation.amount = Decimal("999.00")
            donation.save(update_fields=["amount", "updated_at"])

        # Critical: the parent update must have rolled back.
        donation.refresh_from_db()
        assert donation.amount == original_amount

    def test_invoice_save_failure_rolls_back_parent_row(self) -> None:
        """Invoice update must not persist when audit insert fails inside a txn.

        Invoices are financial artifacts (issue → paid → overdue → cancelled
        state machine, monetary totals, client billing). Losing the audit
        trail of a status / amount change is the same compliance gap as
        Donation, so the failure handling must roll back identically.
        """
        from django.db import transaction

        # Build the Invoice (and supporting rows) outside the patched window
        # so unrelated AuditLog inserts succeed normally.
        user = UserFactory(is_staff=True)
        campaign = CampaignFactory(created_by=user)
        invoice = Invoice.objects.create(
            client=campaign.client,
            campaign=campaign,
            billing_period_start=date.today() - timedelta(days=30),
            billing_period_end=date.today(),
            due_date=date.today() + timedelta(days=30),
            created_by=user,
        )
        original_status = invoice.status

        boom = RuntimeError("audit table locked")
        with (
            patch(
                "audit.models.AuditLog.objects.create",
                side_effect=boom,
            ),
            patch("audit.signals.sentry_sdk.capture_exception") as captured,
            pytest.raises(RuntimeError, match="audit table locked"),
            transaction.atomic(),
        ):
            invoice.status = Invoice.STATUS_ISSUED
            invoice.save(update_fields=["status", "updated_at"])

        # Critical: the parent update must have rolled back.
        invoice.refresh_from_db()
        assert invoice.status == original_status
        captured.assert_called_once()
        assert captured.call_args.args[0] is boom


@pytest.mark.django_db()
class TestAuditPostSaveFailureNonCriticalModels:
    """Non-critical audit failures stay silent for the caller but reach Sentry."""

    def test_campaign_save_succeeds_when_audit_log_create_fails(self) -> None:
        """A Campaign (non-critical) save completes even if audit insert blows up."""
        with (
            patch(
                "audit.models.AuditLog.objects.create",
                side_effect=RuntimeError("audit table locked"),
            ),
            patch("audit.signals.sentry_sdk.capture_exception") as captured,
        ):
            campaign = CampaignFactory()

        # Parent save persisted despite the audit failure.
        assert campaign.pk is not None
        # Sentry was notified for the failed audit insert(s).
        assert captured.call_count >= 1
        for call in captured.call_args_list:
            assert isinstance(call.args[0], Exception)

    def test_non_critical_audit_failure_does_not_create_audit_row(self) -> None:
        """The mocked audit insert really did fail — no audit row landed."""
        with (
            patch(
                "audit.models.AuditLog.objects.create",
                side_effect=RuntimeError("audit table locked"),
            ),
            patch("audit.signals.sentry_sdk.capture_exception"),
        ):
            campaign = CampaignFactory()

        assert not AuditLog.objects.filter(
            model_name="Campaign", object_id=str(campaign.pk)
        ).exists()


@pytest.mark.django_db()
class TestAuditPostSaveSuccessPath:
    """The happy path is unaffected by the new failure handling."""

    def test_successful_donation_save_creates_audit_log_and_skips_sentry(
        self,
    ) -> None:
        """When AuditLog.create succeeds we never call sentry_sdk.capture_exception."""
        with patch("audit.signals.sentry_sdk.capture_exception") as captured:
            donation = DonationFactory(amount=Decimal("5.00"))

        assert AuditLog.objects.filter(
            model_name="Donation",
            action="CREATE",
            object_id=str(donation.pk),
        ).exists()
        # Donation success path — sentry must not have been touched for this
        # save. Other unrelated factory rows may run their own signals, but
        # none of them should have failed either.
        for call in captured.call_args_list:
            # If anything *did* hit sentry, it must not be a Donation failure.
            assert not isinstance(call.args[0], Donation)
        # Tightest assertion: no calls at all in the clean test DB.
        captured.assert_not_called()
