"""Regression tests covering AUDITED_MODELS signal coverage.

Each new model added to ``audit.signals.AUDITED_MODELS`` must be observed
end-to-end: creating an instance should emit a CREATE ``AuditLog`` row whose
``model_name`` matches the model class name.
"""

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from audit.models import AuditLog
from audit.signals import (
    AUDITED_MODELS,
    CRITICAL_AUDITED_MODELS,
    _resolve_audited_model,
)
from clients.models import ClientPortalUser
from donors.models import DonorUpload
from invoices.models import Invoice
from letters.models import LetterBatch, LetterTemplate
from scans.models import ScanBatch
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    PayingInSlipFactory,
    PaymentGatewayConfigFactory,
    ScanBatchFactory,
    StripeCustomerFactory,
    StripePaymentFactory,
    StripePaymentMethodFactory,
    SystemDonorFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestAuditedModelsCoverage:
    """Verify each member of AUDITED_MODELS resolves and emits AuditLog rows."""

    def test_new_audited_models_are_registered(self) -> None:
        """The expanded scan-pipeline models are in AUDITED_MODELS."""
        for name in (
            "LetterBatch",
            "StripePayment",
            "PayingInSlip",
            "Invoice",
            "ScanBatch",
        ):
            assert name in AUDITED_MODELS

    def test_every_audited_model_resolves_to_a_concrete_class(self) -> None:
        """No stale entries: ``_resolve_audited_model`` finds every name."""
        for name in AUDITED_MODELS:
            assert _resolve_audited_model(name) is not None, (
                f"{name} does not resolve to an installed model"
            )

    def test_letter_batch_create_emits_audit_log(self) -> None:
        """Creating a LetterBatch produces a CREATE AuditLog entry."""
        user = UserFactory()
        campaign = CampaignFactory(created_by=user)
        template = LetterTemplate.objects.create(
            campaign=campaign,
            file=SimpleUploadedFile("template.docx", b"fake"),
            created_by=user,
        )

        batch = LetterBatch.objects.create(
            campaign=campaign,
            template=template,
            batch_number=1,
        )

        assert AuditLog.objects.filter(
            model_name="LetterBatch",
            action="CREATE",
            object_id=str(batch.pk),
        ).exists()

    def test_stripe_payment_create_emits_audit_log(self) -> None:
        """Creating a StripePayment produces a CREATE AuditLog entry."""
        payment = StripePaymentFactory()

        assert AuditLog.objects.filter(
            model_name="StripePayment",
            action="CREATE",
            object_id=str(payment.pk),
        ).exists()

    def test_paying_in_slip_create_emits_audit_log(self) -> None:
        """Creating a PayingInSlip produces a CREATE AuditLog entry."""
        slip = PayingInSlipFactory()

        assert AuditLog.objects.filter(
            model_name="PayingInSlip",
            action="CREATE",
            object_id=str(slip.pk),
        ).exists()

    def test_invoice_create_emits_audit_log(self) -> None:
        """Creating an Invoice produces a CREATE AuditLog entry."""
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

        assert AuditLog.objects.filter(
            model_name="Invoice",
            action="CREATE",
            object_id=str(invoice.pk),
        ).exists()

    def test_scan_batch_create_emits_audit_log(self) -> None:
        """Creating a ScanBatch produces a CREATE AuditLog entry."""
        batch: ScanBatch = ScanBatchFactory()

        assert AuditLog.objects.filter(
            model_name="ScanBatch",
            action="CREATE",
            object_id=str(batch.pk),
        ).exists()

    # ─── Coverage extension (audit 2026-05-02 §7.1) ───────────────────────

    def test_audit_2026_additions_are_registered(self) -> None:
        """Models added by the 2026-05-02 audit are in AUDITED_MODELS."""
        for name in (
            "ClientPortalUser",
            "DonorUpload",
            "PaymentGatewayConfig",
            "StripeCustomer",
            "StripePaymentMethod",
            "SystemDonor",
        ):
            assert name in AUDITED_MODELS

    def test_payment_gateway_config_is_critical(self) -> None:
        """PaymentGatewayConfig holds Stripe webhook secrets — must be critical."""
        assert "PaymentGatewayConfig" in CRITICAL_AUDITED_MODELS

    def test_stripe_webhook_event_intentionally_excluded(self) -> None:
        """StripeWebhookEvent is intentionally not audited (volume + already immutable)."""
        assert "StripeWebhookEvent" not in AUDITED_MODELS

    def test_payment_gateway_config_create_emits_audit_log(self) -> None:
        """Creating a PaymentGatewayConfig produces a CREATE AuditLog entry."""
        config = PaymentGatewayConfigFactory()
        assert AuditLog.objects.filter(
            model_name="PaymentGatewayConfig",
            action="CREATE",
            object_id=str(config.pk),
        ).exists()

    def test_stripe_customer_create_emits_audit_log(self) -> None:
        """Creating a StripeCustomer produces a CREATE AuditLog entry."""
        customer = StripeCustomerFactory()
        assert AuditLog.objects.filter(
            model_name="StripeCustomer",
            action="CREATE",
            object_id=str(customer.pk),
        ).exists()

    def test_stripe_payment_method_create_emits_audit_log(self) -> None:
        """Creating a StripePaymentMethod produces a CREATE AuditLog entry."""
        pm = StripePaymentMethodFactory()
        assert AuditLog.objects.filter(
            model_name="StripePaymentMethod",
            action="CREATE",
            object_id=str(pm.pk),
        ).exists()

    def test_system_donor_create_emits_audit_log(self) -> None:
        """Creating a SystemDonor produces a CREATE AuditLog entry."""
        sd = SystemDonorFactory()
        assert AuditLog.objects.filter(
            model_name="SystemDonor",
            action="CREATE",
            object_id=str(sd.pk),
        ).exists()

    def test_donor_upload_create_emits_audit_log(self) -> None:
        """Creating a DonorUpload produces a CREATE AuditLog entry."""
        user = UserFactory()
        campaign = CampaignFactory(created_by=user)
        upload = DonorUpload.objects.create(
            campaign=campaign,
            file=SimpleUploadedFile("donors.csv", b"urn,name\n1,Test\n"),
            uploaded_by=user,
        )
        assert AuditLog.objects.filter(
            model_name="DonorUpload",
            action="CREATE",
            object_id=str(upload.pk),
        ).exists()

    def test_client_portal_user_create_emits_audit_log(self) -> None:
        """Creating a ClientPortalUser produces a CREATE AuditLog entry."""
        client = ClientFactory()
        portal_user = UserFactory()
        cpu = ClientPortalUser.objects.create(
            client=client,
            user=portal_user,
            role="viewer",
        )
        assert AuditLog.objects.filter(
            model_name="ClientPortalUser",
            action="CREATE",
            object_id=str(cpu.pk),
        ).exists()
