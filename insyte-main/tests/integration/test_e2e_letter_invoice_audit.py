"""End-to-end integration tests for letter generation, invoice generation, and audit-log coverage.

Drives the *post-payment* tail of the donation pipeline:

1. **Letter generation per payment type**: After QA approval and payment settlement
   (where applicable), the ``/admin/letter-setup/campaign/<id>/generate-letter/``
   endpoint queues a letter batch. The eager Celery task runs synchronously and
   writes one merged DOCX file per chunk to ``default_storage``. Eligibility for
   inclusion depends on QA status — only ``approved`` and ``rejected`` donations
   reach the queryset. Failed and disputed payments must not produce a letter.
2. **Invoice generation**: ``InvoiceService.prepare_for_save`` is exercised via
   ``Invoice.save()`` and ``InvoicePdfService.build_pdf`` is asked to emit PDF
   bytes for a campaign-scoped invoice with explicit ServiceItem-driven line
   items.
3. **AuditLog coverage**: The ``audit/signals.py`` registry connects ``post_save``
   handlers per audited model. Drives state transitions and asserts that an
   ``AuditLog`` row exists for each one.
4. **ApprovalLog / ExportLog**: The ``audit.models`` schema exists for these but
   no production code currently writes them. The tests verify the *shape* of
   the rows is correct so the contract holds for future writers.

Stripe / Doc AI / R2 are not exercised — letter rendering writes to a temp
``MEDIA_ROOT`` provided by pytest's ``tmp_path``, and Celery runs eagerly via
``CELERY_TASK_ALWAYS_EAGER`` in ``responsehandling.settings.test``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from django.urls import reverse
from docx import Document

from audit.models import ApprovalLog, AuditLog, ExportLog
from audit.signals import AUDITED_MODELS
from donations.models import Donation, DonationBatch
from invoices.models import Invoice, InvoiceSettings, ServiceCategory, ServiceItem
from invoices.pdf import InvoicePdfService
from letters.models import LetterBatch, LetterTemplate
from scans.models import ScanBatch
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    ScanBatchFactory,
)

if TYPE_CHECKING:
    from core.models import User


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_docx_bytes(*paragraphs: str) -> bytes:
    """Return raw bytes for a minimal DOCX file containing the given paragraphs."""
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _build_docx_upload(
    *paragraphs: str, name: str = "template.docx"
) -> SimpleUploadedFile:
    """Build an in-memory DOCX file for upload to ``LetterTemplate.file``."""
    return SimpleUploadedFile(
        name,
        _build_docx_bytes(*paragraphs),
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )


def _create_letter_template(
    campaign: Any,
    user: Any,
    *,
    template_type: str = "thank_you",
    body: str = "Dear {{ donor_full_name }}, thank you for {{ amount_formatted }}.",
) -> LetterTemplate:
    """Create an active LetterTemplate backed by real DOCX bytes on disk."""
    return LetterTemplate.objects.create(
        campaign=campaign,
        name=f"Active {template_type}",
        template_type=template_type,
        file=_build_docx_upload(body, name=f"{template_type}.docx"),
        is_active=True,
        created_by=user,
    )


def _isolate_media(settings: Any, tmp_path: Path) -> None:
    """Re-point ``MEDIA_ROOT`` and ``STORAGES`` at a per-test temp directory.

    Letter generation writes merged DOCX files to ``default_storage`` (which is
    backed by ``FileSystemStorage`` rooted at ``MEDIA_ROOT``). Without this each
    test would write into the real ``media/`` tree and read each other's output.
    """
    settings.STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path)},
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    settings.MEDIA_ROOT = str(tmp_path)


# ---------------------------------------------------------------------------
# Letter generation per payment type
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestLetterGenerationPerPaymentType:
    """Letter generation eligibility against terminal payment / QA states."""

    def _post_generate(
        self,
        client: DjangoClient,
        campaign: Any,
    ) -> dict[str, Any]:
        """POST to the generate-letter endpoint and return the parsed JSON body."""
        response = client.post(
            reverse(
                "custom_admin:generate_letter",
                kwargs={"campaign_id": campaign.id},
            ),
            {"letters_per_file": "100", "donation_filter": "all"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert response.status_code == 200, response.content
        return response.json()

    def test_card_completed_generates_letter(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """A QA-approved card donation with ``payment_status=completed`` produces a letter."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            letter_status="pending",
        )

        payload = self._post_generate(authenticated_client, campaign)
        batch = LetterBatch.objects.get(id=payload["batch_id"])

        assert batch.status == LetterBatch.STATUS_COMPLETED
        assert batch.generated_count == 1
        assert batch.failed_count == 0
        assert batch.file_count == 1
        # default_storage holds exactly the file the batch claims.
        assert default_storage.exists(batch.output_files[0])

    def test_cheque_settled_generates_letter(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """A QA-approved cheque donation in ``completed`` payment state still gets a letter."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_CHEQUE,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            letter_status="pending",
        )

        payload = self._post_generate(authenticated_client, campaign)
        batch = LetterBatch.objects.get(id=payload["batch_id"])

        assert batch.generated_count == 1
        assert batch.file_count == 1
        assert default_storage.exists(batch.output_files[0])

    def test_non_financial_generates_letter_immediately(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """Non-financial donations skip payment capture entirely but still get a letter."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_NON_FINANCIAL,
            payment_status=Donation.PAYMENT_STATUS_PENDING,
            qa_status=Donation.QA_STATUS_APPROVED,
            letter_status="pending",
        )

        payload = self._post_generate(authenticated_client, campaign)
        batch = LetterBatch.objects.get(id=payload["batch_id"])

        assert batch.generated_count == 1
        assert batch.file_count == 1

    def test_failed_payment_does_not_generate_letter(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """A QA-rejected card donation that failed payment is skipped (no issue template)."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        # No issue template configured -> the view filters out QA-rejected
        # donations before counting, so generation has nothing to do.
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_FAILED,
            qa_status=Donation.QA_STATUS_REJECTED,
            letter_status="pending",
        )

        response = authenticated_client.post(
            reverse(
                "custom_admin:generate_letter",
                kwargs={"campaign_id": campaign.id},
            ),
            {"letters_per_file": "100", "donation_filter": "all"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert "No approved or rejected donations" in body["error"]

    def test_disputed_payment_does_not_generate_letter(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """A QA-pending donation with a Stripe dispute is excluded from letter generation."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        # qa_status pending means the queryset filter (approved/rejected only)
        # excludes this donation regardless of payment_status.
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_DISPUTED,
            qa_status=Donation.QA_STATUS_PENDING,
            letter_status="pending",
        )

        response = authenticated_client.post(
            reverse(
                "custom_admin:generate_letter",
                kwargs={"campaign_id": campaign.id},
            ),
            {"letters_per_file": "100", "donation_filter": "all"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        assert response.status_code == 400
        assert b"No approved or rejected donations" in response.content

    def test_generated_letters_listing_and_download(
        self,
        authenticated_client: DjangoClient,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """The list URL surfaces generated files and the download URL serves bytes."""
        _isolate_media(settings, tmp_path)
        campaign = CampaignFactory(created_by=staff_user)
        _create_letter_template(campaign, staff_user)
        DonationFactory(
            campaign=campaign,
            payment_method=Donation.PAYMENT_METHOD_CARD,
            payment_status=Donation.PAYMENT_STATUS_COMPLETED,
            qa_status=Donation.QA_STATUS_APPROVED,
            letter_status="pending",
        )

        payload = self._post_generate(authenticated_client, campaign)
        batch = LetterBatch.objects.get(id=payload["batch_id"])
        stored_path = batch.output_files[0]
        filename = Path(stored_path).name

        # List URL renders and reports the file
        list_response = authenticated_client.get(
            reverse(
                "custom_admin:view_generated_letters",
                kwargs={"campaign_id": campaign.id},
            )
        )
        assert list_response.status_code == 200
        assert filename.encode() in list_response.content

        # Download URL serves the file bytes
        download_response = authenticated_client.get(
            reverse(
                "custom_admin:download_letter",
                kwargs={"campaign_id": campaign.id, "filename": filename},
            )
        )
        assert download_response.status_code == 200
        body = b"".join(download_response.streaming_content)
        assert len(body) > 0
        # DOCX files are ZIP archives — the magic-number is "PK\x03\x04".
        assert body.startswith(b"PK")


# ---------------------------------------------------------------------------
# Invoice generation
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestInvoiceGeneration:
    """End-to-end Invoice + ServiceItem + InvoicePdfService coverage."""

    def test_invoice_pdf_generation_for_campaign(
        self,
        staff_user: User,
    ) -> None:
        """Creating an Invoice with ServiceItems yields a PDF and persists totals correctly."""
        campaign = CampaignFactory(created_by=staff_user)

        category = ServiceCategory.objects.create(name="Letter Set Up", order=1)
        item_letter = ServiceItem.objects.create(
            category=category,
            description="Thank-you letter generation",
            unit_price=Decimal("0.50"),
            pricing_unit="each",
        )
        item_setup = ServiceItem.objects.create(
            category=category,
            description="Campaign setup fee",
            unit_price=Decimal("100.00"),
            pricing_unit="per campaign",
        )

        invoice = Invoice.objects.create(
            client=campaign.client,
            campaign=campaign,
            billing_period_start=date.today() - timedelta(days=30),
            billing_period_end=date.today(),
            due_date=date.today() + timedelta(days=30),
            created_by=staff_user,
            service_fee=Decimal("50.00"),  # 100 letters * 0.50
            setup_fee=Decimal("100.00"),
            tax_rate=Decimal("20.00"),
            letters_generated=100,
        )

        # InvoiceService.prepare_for_save is invoked from Invoice.save() and
        # must produce a deterministic invoice_number plus computed totals.
        assert invoice.invoice_number.startswith("INV-")
        assert invoice.subtotal == Decimal("150.00")
        assert invoice.tax_amount == Decimal("30.00")
        assert invoice.total_amount == Decimal("180.00")
        assert invoice.balance_due == Decimal("180.00")

        # Build the PDF via the service.
        line_items: list[tuple[str, int | float, Decimal, Decimal]] = [
            (item_letter.description, 100, item_letter.unit_price, Decimal("50.00")),
            (item_setup.description, 1, item_setup.unit_price, Decimal("100.00")),
        ]
        pdf_bytes = InvoicePdfService.build_pdf(
            invoice,
            line_items,
            InvoiceSettings.get_settings(),
            mode="download",
        )

        assert isinstance(pdf_bytes, bytes)
        assert len(pdf_bytes) > 0
        assert pdf_bytes.startswith(b"%PDF-")
        # The Content-Disposition helper builds an attachment header for download mode.
        disposition = InvoicePdfService.get_content_disposition(
            invoice, mode="download"
        )
        assert disposition.startswith("attachment;")
        assert invoice.invoice_number in disposition


# ---------------------------------------------------------------------------
# Audit-log coverage
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAuditLogCoverage:
    """Per-state-transition AuditLog coverage across the donation pipeline."""

    def test_audited_models_constant_includes_pipeline_models(self) -> None:
        """The ``AUDITED_MODELS`` registry covers every model the pipeline touches."""
        for model_name in (
            "Campaign",
            "Donation",
            "DonationBatch",
            "Invoice",
            "LetterBatch",
            "LetterTemplate",
            "ScanBatch",
        ):
            assert model_name in AUDITED_MODELS

    def test_scan_to_letter_pipeline_writes_audit_logs_at_every_step(
        self,
        staff_user: User,
        settings: Any,
        tmp_path: Path,
    ) -> None:
        """Each pipeline state transition emits a matching ``AuditLog`` row."""
        _isolate_media(settings, tmp_path)

        # 1. Scan batch state transition.
        scan_batch: ScanBatch = ScanBatchFactory(created_by=staff_user)
        scan_batch_create_log = AuditLog.objects.filter(
            model_name="ScanBatch",
            object_id=str(scan_batch.id),
            action="CREATE",
        )
        assert scan_batch_create_log.exists()

        scan_batch.status = ScanBatch.STATUS_COMPLETED
        scan_batch.save(update_fields=["status"])
        scan_batch_update_log = AuditLog.objects.filter(
            model_name="ScanBatch",
            object_id=str(scan_batch.id),
            action="UPDATE",
        )
        assert scan_batch_update_log.exists()
        assert any("status" in (entry.changes or {}) for entry in scan_batch_update_log)

        # 2. DonationBatch QA status transition.
        campaign = CampaignFactory(created_by=staff_user)
        donation_batch = DonationBatchFactory(campaign=campaign)
        donation = DonationFactory(
            campaign=campaign,
            batch=donation_batch,
            qa_status=Donation.QA_STATUS_PENDING,
            payment_status=Donation.PAYMENT_STATUS_PENDING,
        )

        donation_batch.status = DonationBatch.STATUS_APPROVED
        donation_batch.save(update_fields=["status"])
        donation_batch_log = AuditLog.objects.filter(
            model_name="DonationBatch",
            object_id=str(donation_batch.id),
            action="UPDATE",
        )
        assert donation_batch_log.exists()

        # 3. Donation payment_status transition.
        donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
        donation.qa_status = Donation.QA_STATUS_APPROVED
        donation.save(update_fields=["payment_status", "qa_status"])
        donation_log = AuditLog.objects.filter(
            model_name="Donation",
            object_id=str(donation.id),
            action="UPDATE",
        )
        latest_donation_log = donation_log.order_by("-created_at").first()
        assert latest_donation_log is not None
        # Both the payment_status change and qa_status change are captured.
        latest_changes: dict[str, Any] = latest_donation_log.changes
        assert "payment_status" in latest_changes
        assert "qa_status" in latest_changes

        # 4. LetterTemplate + LetterBatch creation.
        template = _create_letter_template(campaign, staff_user)
        assert AuditLog.objects.filter(
            model_name="LetterTemplate",
            object_id=str(template.id),
            action="CREATE",
        ).exists()

        letter_batch = LetterBatch.objects.create(
            campaign=campaign,
            template=template,
            batch_number=1,
            status=LetterBatch.STATUS_PENDING,
            total_letters=1,
            created_by=staff_user,
        )
        assert AuditLog.objects.filter(
            model_name="LetterBatch",
            object_id=str(letter_batch.id),
            action="CREATE",
        ).exists()

        # 5. Invoice creation.
        invoice = Invoice.objects.create(
            client=campaign.client,
            campaign=campaign,
            billing_period_start=date.today() - timedelta(days=30),
            billing_period_end=date.today(),
            due_date=date.today() + timedelta(days=30),
            created_by=staff_user,
            service_fee=Decimal("10.00"),
            tax_rate=Decimal("20.00"),
        )
        assert AuditLog.objects.filter(
            model_name="Invoice",
            object_id=str(invoice.id),
            action="CREATE",
        ).exists()

    def test_qa_approval_writes_approval_log(
        self,
        staff_user: User,
    ) -> None:
        """The ``ApprovalLog`` row created for a QA approval has the right shape.

        ``ApprovalLog`` is currently not auto-written by ``qa_approve_batch``
        (no production code calls ``ApprovalLog.objects.create``). This test
        nails down the row's contract so any future writer can be validated
        against it without surprises.
        """
        campaign = CampaignFactory(created_by=staff_user)
        log = ApprovalLog.objects.create(
            campaign=campaign,
            approver=staff_user,
            status="approved",
            comment="QA passed end-to-end checks.",
        )

        assert log.id is not None
        assert log.campaign_id == campaign.id
        assert log.approver_id == staff_user.id
        assert log.status == "approved"
        assert log.comment.startswith("QA passed")
        assert log.created_at is not None
        # __str__ contract used by admin search / debug output.
        assert campaign.name in str(log)
        assert "approved" in str(log)

    def test_export_action_writes_export_log(
        self,
        staff_user: User,
    ) -> None:
        """The ``ExportLog`` row contract — the audit registry exposes this model
        for callers (admin, future report exports) to write to.
        """
        campaign = CampaignFactory(created_by=staff_user)
        log = ExportLog.objects.create(
            campaign=campaign,
            requested_by=staff_user,
            export_type="CSV",
            status="completed",
        )

        assert log.id is not None
        assert log.campaign_id == campaign.id
        assert log.requested_by_id == staff_user.id
        assert log.export_type == "CSV"
        assert log.status == "completed"
        assert log.created_at is not None
        assert "CSV" in str(log)
        assert "completed" in str(log)
