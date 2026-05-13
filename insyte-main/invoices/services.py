"""Invoice service for handling invoice operations and payment integration.

Provides invoice management, PDF generation, and email receipt functionality.
"""

import logging
import zlib
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import connection
from django.template.loader import render_to_string
from django.utils import timezone

if TYPE_CHECKING:
    from core.models import User
    from invoices.models import Invoice
    from invoices.pdf import LineItem
    from payments.models import StripePayment

logger = logging.getLogger(__name__)


class InvoiceService:
    """Service for invoice and payment integration operations."""

    @staticmethod
    def mark_as_paid(invoice: Invoice, payment: StripePayment) -> None:
        """Mark invoice as paid after successful payment.

        Args:
            invoice: Invoice instance
            payment: StripePayment instance
        """
        invoice.payment_status = "paid"
        invoice.status = "paid"
        invoice.payment_date = timezone.now().date()
        invoice.payment_method = "credit_card"
        invoice.payment_reference = payment.stripe_payment_intent_id
        invoice.amount_paid = payment.amount
        invoice.balance_due = 0
        invoice.save()

        logger.info("Marked invoice %s as paid", invoice.invoice_number)

    @staticmethod
    def send_receipt_email(invoice: Invoice, payment: StripePayment) -> bool:
        """Send payment receipt email to client.

        Args:
            invoice: Invoice instance
            payment: StripePayment instance

        Returns:
            True if email sent successfully, False otherwise
        """
        try:
            # Render email template
            context = {
                "invoice": invoice,
                "payment": payment,
                "client": invoice.client,
            }

            html_content = render_to_string(
                "admin/emails/payment_receipt.html", context
            )
            text_content = render_to_string("admin/emails/payment_receipt.txt", context)

            # Use EmailMultiAlternatives to properly send both text and HTML
            from django.core.mail import EmailMultiAlternatives

            email = EmailMultiAlternatives(
                subject=f"Payment Receipt - Invoice {invoice.invoice_number}",
                body=text_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[invoice.client.email],
            )
            email.attach_alternative(html_content, "text/html")
            email.send()

            logger.info(
                f"Sent receipt email for invoice {invoice.invoice_number} to {invoice.client.email}"
            )
            return True

        except Exception as e:
            logger.error("Failed to send receipt email: %s", e)
            return False

    @staticmethod
    def send_payment_failed_email(invoice: Invoice, error_message: str) -> bool:
        """Send payment failed notification to client.

        Args:
            invoice: Invoice instance
            error_message: Error message from payment processor

        Returns:
            True if email sent successfully, False otherwise
        """
        try:
            context = {
                "invoice": invoice,
                "error_message": error_message,
                "client": invoice.client,
            }

            html_content = render_to_string("admin/emails/payment_failed.html", context)
            text_content = render_to_string("admin/emails/payment_failed.txt", context)

            from django.core.mail import EmailMultiAlternatives

            email = EmailMultiAlternatives(
                subject=f"Payment Failed - Invoice {invoice.invoice_number}",
                body=text_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[invoice.client.email],
            )
            email.attach_alternative(html_content, "text/html")
            email.send()

            logger.info(
                f"Sent payment failed email for invoice {invoice.invoice_number}"
            )
            return True

        except Exception as e:
            logger.error("Failed to send payment failed email: %s", e)
            return False

    @staticmethod
    def send_invoice_by_email(
        invoice: Invoice,
        *,
        to: list[str],
        cc: list[str] | None = None,
        body: str | None = None,
        sender: User,
    ) -> None:
        """Email an invoice PDF to one or more recipients.

        Generates the invoice PDF on the fly and attaches it to an
        ``EmailMessage`` routed through Django's configured ``EMAIL_BACKEND``
        (Resend in production, console in development, locmem in tests).

        Args:
            invoice: Invoice to send.
            to: Non-empty list of primary recipient email addresses.
            cc: Optional list of CC recipient email addresses.
            body: Optional operator-supplied message included after the
                default greeting.
            sender: Staff user initiating the send (used for audit logging).

        Raises:
            ValidationError: If ``to`` is empty or any address in ``to`` /
                ``cc`` is not a syntactically valid email.
        """
        from audit.utils import log_action
        from invoices.models import InvoiceSettings
        from invoices.pdf import InvoicePdfService
        from invoices.utils import get_invoice_line_items

        cc_list = list(cc or [])

        if not to:
            msg = "At least one recipient address is required."
            raise ValidationError(msg)

        for address in [*to, *cc_list]:
            validate_email(address)

        inv_settings = InvoiceSettings.get_settings()
        line_items: list[LineItem] = list(get_invoice_line_items(invoice, inv_settings))
        pdf_bytes = InvoicePdfService.build_pdf(
            invoice, line_items, inv_settings, mode="download"
        )

        company_name = inv_settings.company_name or "CFP Insyte Portal"
        subject = f"Invoice {invoice.invoice_number} from {company_name}"

        intro = (
            f"Dear {invoice.client.name},\n\n"
            f"Please find attached invoice {invoice.invoice_number} "
            f"for the period {invoice.billing_period_start:%d %B %Y} to "
            f"{invoice.billing_period_end:%d %B %Y}.\n"
        )
        operator_message = f"\n{body.strip()}\n" if body and body.strip() else ""
        outro = (
            f"\nTotal due: £{invoice.total_amount:.2f}\n"
            f"Due date: {invoice.due_date:%d %B %Y}\n\n"
            f"Kind regards,\n{company_name}\n"
        )
        message_body = intro + operator_message + outro

        email = EmailMessage(
            subject=subject,
            body=message_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=list(to),
            cc=cc_list,
        )
        email.attach(
            f"invoice_{invoice.invoice_number}.pdf",
            pdf_bytes,
            "application/pdf",
        )
        email.send(fail_silently=False)

        recipients_summary = ", ".join([*to, *cc_list])
        log_action(
            user=sender,
            action="EMAIL",
            model_name="Invoice",
            object_id=str(invoice.id),
            object_repr=f"Invoice {invoice.invoice_number}",
            summary=(
                f"Emailed invoice {invoice.invoice_number} to {recipients_summary}"
            ),
        )
        logger.info(
            "Sent invoice %s by email to %s",
            invoice.invoice_number,
            recipients_summary,
        )

    @staticmethod
    def _allocate_invoice_number_if_missing(invoice: Invoice) -> None:
        """Populate ``invoice_number`` when absent.

        Runs inside ``Invoice.save()``'s enclosing ``transaction.atomic()`` so
        PostgreSQL ``pg_advisory_xact_lock`` remains held until the invoice row
        is written.

        Concurrency:
            * **PostgreSQL**: ``pg_advisory_xact_lock`` serializes allocation for
              each ``INV-YYYY-MM`` prefix, including the ``count == 0`` case where
              ``SELECT … FOR UPDATE`` would lock no rows.
            * **Other backends**: Allocation still uses ``select_for_update()`` on
              existing rows plus a collision-resolution loop; SQLite serializes
              writers, which matches typical test / low-concurrency setups.
        """
        from invoices.models import Invoice as InvoiceModel

        if invoice.invoice_number:
            return

        now = timezone.now()
        prefix = f"INV-{now.year}-{now.month:02d}"

        if connection.vendor == "postgresql":
            lock_key = zlib.crc32(prefix.encode()) & 0x7FFFFFFF
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])

        qs = InvoiceModel.objects.select_for_update().filter(
            invoice_number__startswith=prefix,
        )
        if invoice.pk:
            qs = qs.exclude(pk=invoice.pk)
        month_count = qs.count()

        candidate_seq = month_count + 1
        for _ in range(10_000):
            candidate = f"{prefix}-{candidate_seq:04d}"
            clash = InvoiceModel.objects.filter(invoice_number=candidate)
            if invoice.pk:
                clash = clash.exclude(pk=invoice.pk)
            if not clash.exists():
                invoice.invoice_number = candidate
                return
            candidate_seq += 1

        msg = (
            "Unable to allocate a unique invoice_number after exhaustive attempts; "
            "contact support."
        )
        raise ValidationError(msg)

    @staticmethod
    def prepare_for_save(invoice: Invoice) -> None:
        """Prepare invoice fields before saving: number generation, totals, status.

        Called from ``Invoice.save()`` before ``super().save()``.  Mutates
        the invoice instance in-place without persisting it.

        ``Invoice.save`` wraps preparation and persistence in ``atomic()`` so
        invoice numbering locks remain valid through insert/update.

        Args:
            invoice: Invoice instance about to be saved.
        """
        from invoices.models import Invoice as InvoiceModel

        InvoiceService._allocate_invoice_number_if_missing(invoice)

        invoice.subtotal = (
            invoice.service_fee
            + invoice.processing_fee
            + invoice.setup_fee
            + invoice.additional_charges
            - invoice.discount_amount
        )
        invoice.tax_amount = invoice.subtotal * (invoice.tax_rate / Decimal("100"))
        invoice.total_amount = invoice.subtotal + invoice.tax_amount
        invoice.balance_due = invoice.total_amount - invoice.amount_paid

        today = timezone.localdate()

        paid_in_full = (
            invoice.amount_paid >= invoice.total_amount and invoice.total_amount > 0
        )
        if paid_in_full:
            invoice.status = InvoiceModel.STATUS_PAID
            return

        overdue = (
            invoice.status == InvoiceModel.STATUS_ISSUED
            and invoice.due_date is not None
            and invoice.due_date < today
            and invoice.balance_due > Decimal("0")
        )
        if overdue:
            invoice.status = InvoiceModel.STATUS_OVERDUE
