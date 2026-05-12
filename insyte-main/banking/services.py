"""Banking service for PayingInSlip business logic.

Centralises all business operations on paying-in slips that require
database queries, state mutations, or external imports — keeping
the :model:`core.PayingInSlip` model thin.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from donations.models import Donation

if TYPE_CHECKING:
    from banking.models import PayingInSlip
    from clients.models import Client

logger = logging.getLogger(__name__)


# Card donations stay unsettled until Stripe confirms; counting them on a slip
# would inflate the deposit total. Non-card payment methods do not maintain
# payment_status, so they are always considered settled for banking purposes.
SLIP_SETTLED_DONATION_Q = ~Q(payment_method=Donation.PAYMENT_METHOD_CARD) | Q(
    payment_status=Donation.PAYMENT_STATUS_COMPLETED
)


# Completion statuses that mean "the bank rejected some/all of the deposit",
# triggering automatic donation-payment reversal in ``slip_record_processing``.
# Kept here (next to the service) so the view doesn't hardcode the set.
SLIP_REVERSAL_COMPLETION_STATUSES: frozenset[str] = frozenset(
    {"issues", "partial_success", "failed"}
)


class BankingService:
    """Service for paying-in slip operations."""

    @staticmethod
    def recalculate_slip_totals(slip: PayingInSlip) -> None:
        """Recalculate total amount and item count from settled donations.

        Donations whose payment has not settled (card payments still
        ``pending``/``failed``/``refunded``) are excluded so they cannot
        inflate the deposit total.

        Args:
            slip: PayingInSlip instance to update.
        """
        from django.db.models import Sum

        aggregates = slip.donations.filter(SLIP_SETTLED_DONATION_Q).aggregate(
            total=Sum("amount"),
            count=models.Count("id"),
        )
        slip.total_amount = aggregates["total"] or Decimal(0)
        slip.total_items = aggregates["count"] or 0
        slip.save(update_fields=["total_amount", "total_items", "updated_at"])

    @staticmethod
    def mark_slip_as_processed(
        slip: PayingInSlip,
        processed_amount: Decimal,
        completion_status: str,
        bank_processed_date: Any,
        processing_issues: list[str] | None = None,
        custom_issue: str = "",
        processed_by: Any = None,
    ) -> None:
        """Mark slip as processed with bank results.

        Args:
            slip: PayingInSlip instance to update.
            processed_amount: Amount successfully processed.
            completion_status: Processing outcome status.
            bank_processed_date: Date bank processed the slip.
            processing_issues: List of issue codes.
            custom_issue: Custom issue description.
            processed_by: User recording the results.
        """
        slip.processed_amount = processed_amount
        slip.completion_status = completion_status
        slip.bank_processed_date = bank_processed_date
        slip.processing_issues = processing_issues or []
        slip.custom_issue = custom_issue
        slip.processed_by = processed_by
        slip.processed_at = timezone.now()

        if completion_status == "full_success":
            slip.status = "processed"
        elif completion_status in ["partial_success", "issues"]:
            slip.status = "partially_processed"
        else:
            slip.status = "failed"

        slip.save()

    @staticmethod
    def reverse_donations_for_slip(slip: PayingInSlip, reason: str) -> int:
        """Mark every settled donation on ``slip`` as ``reversed``.

        Used when bank processing flagged a slip as ``issues`` /
        ``partial_success`` / ``failed`` (cheque bounced, cash count short,
        etc.). Without this, donations stay at ``payment_status="completed"``
        even though the bank rejected the deposit — operators have no way
        to find them and thank-you letters go out for non-existent money.

        Walks ``Donation.objects.filter(paying_in_slip=slip,
        payment_status="completed")`` under ``transaction.atomic`` with
        ``select_for_update`` so a concurrent QA approval can't flip the
        same row to ``completed`` mid-reversal. Each row's
        ``payment_status`` becomes ``reversed`` and ``payment_reversal_reason``
        records the operator-supplied reason. The standard ``post_save``
        audit signal fires per row, so the reversal is fully captured in
        ``AuditLog`` without a manual write here.

        Cascade: when a donation's ``letter_status`` is ``generated`` or
        ``sent`` at reversal time, ``letter_voided_at`` and
        ``letter_void_reason`` are populated on the same row. The original
        ``letter_status`` is preserved (a physical letter went out — that
        history matters); ``letter_voided_at IS NOT NULL`` is the
        authoritative "letter no longer valid" signal for downstream
        consumers. After commit, ``_notify_reversal_cascade`` fans out
        in-app notifications and emails to the slip processor, the print
        operator (via ``letter_batch.created_by``), the QA reviewer, and
        the batch creator.

        Stripe/DB divergence: card donations on a slip are eligible for
        reversal (the filter is by ``payment_status``, not method). For
        card rows, this method does NOT issue a Stripe refund — the
        ``payment_status="reversed"`` flag is a DB-side signal only, and
        ``StripePayment.status`` remains the source of truth for the
        actual Stripe-held funds until an operator issues a refund (which
        lands via the ``charge.refunded`` webhook and flips this field to
        ``refunded``). The cascade notification surfaces the manual-refund
        prompt for these cases.

        Idempotent: rerunning skips rows already at ``reversed``; the
        ``letter_voided_at IS NULL`` guard prevents stomping a previously
        recorded void timestamp on a re-run.

        Args:
            slip: The PayingInSlip whose donations should be reversed.
            reason: Operator-supplied free-text explanation. Stored on
                each donation's ``payment_reversal_reason`` field and on
                ``letter_void_reason`` for any cascaded letters.

        Returns:
            int: Number of donations reversed in this call.
        """
        with transaction.atomic():
            donations = list(
                Donation.objects.select_for_update().filter(
                    paying_in_slip=slip,
                    payment_status=Donation.PAYMENT_STATUS_COMPLETED,
                )
            )
            if not donations:
                return 0

            now = timezone.now()
            for donation in donations:
                donation.payment_status = Donation.PAYMENT_STATUS_REVERSED
                donation.payment_reversal_reason = reason
                update_fields = [
                    "payment_status",
                    "payment_reversal_reason",
                    "updated_at",
                ]
                # Void any already-issued letter. ``letter_status`` itself is
                # preserved — the physical letter went out, so reporting "this
                # was sent but is now invalid" is more truthful than rewriting
                # the original status.
                if (
                    donation.letter_status in ("generated", "sent")
                    and donation.letter_voided_at is None
                ):
                    donation.letter_voided_at = now
                    donation.letter_void_reason = reason
                    update_fields += ["letter_voided_at", "letter_void_reason"]
                donation.save(update_fields=update_fields)

        # Fan out notifications + emails after the DB commit so a rolled-back
        # reversal never leaks alerts to operators. Passing donation ids
        # explicitly (rather than re-querying ``slip.donations.filter(reversed)``)
        # is correct when the same slip has previously-reversed rows from an
        # earlier cycle — we only want to notify about rows reversed in this
        # call.
        slip_id = slip.id
        donation_ids = [donation.id for donation in donations]
        transaction.on_commit(
            lambda: BankingService._notify_reversal_cascade(
                slip_id, donation_ids, reason
            )
        )

        logger.info(
            "Reversed %d donation(s) for slip %s: %s",
            len(donations),
            slip.slip_number,
            reason,
        )
        return len(donations)

    @staticmethod
    def _notify_reversal_cascade(
        slip_id: int,
        donation_ids: list[Any],
        reason: str,
    ) -> None:
        """Fan out in-app notifications + emails for a banking reversal.

        Runs after the reversal transaction commits. Operates on the donation
        ids reversed in *this* call (not all reversed donations on the slip),
        so a re-cycle of the same slip notifies only on the new rows.

        Routing (no new Django groups required, deduped by user.id):
            - ``slip.processed_by`` always — they just submitted the bank result.
            - For each donation whose letter was voided in the cascade,
              ``donation.letter_batch.created_by`` (the print operator who
              issued the letter — the most actionable recipient).
            - For each affected batch: ``batch.reviewed_by`` and
              ``batch.created_by``.

        Args:
            slip_id: ID of the PayingInSlip being processed.
            donation_ids: Donation ids reversed in this call.
            reason: Operator-supplied reversal reason.
        """
        from banking.models import PayingInSlip
        from core.tasks import send_batch_status_email
        from donations.models import Donation
        from notifications.models import Notification

        try:
            slip = PayingInSlip.objects.select_related("client", "processed_by").get(
                id=slip_id
            )
        except PayingInSlip.DoesNotExist:
            logger.warning(
                "Reversal-cascade notification skipped: slip %s missing", slip_id
            )
            return

        donations = list(
            Donation.objects.select_related(
                "batch",
                "batch__created_by",
                "batch__reviewed_by",
                "letter_batch",
                "letter_batch__created_by",
            ).filter(id__in=donation_ids)
        )
        if not donations:
            return

        letters_voided = [d for d in donations if d.letter_voided_at is not None]
        gift_aid_retractions = [
            d
            for d in donations
            if d.gift_aid and d.batch.gift_aid_submitted_at is not None
        ]
        card_refunds_pending = [
            d for d in donations if d.payment_method == Donation.PAYMENT_METHOD_CARD
        ]

        # Severity: error if any card-refund flag (financial drift), else
        # warning if anything escalated (letters or HMRC retraction), else
        # info for the bare-minimum reversal record.
        if card_refunds_pending:
            notif_type = Notification.TYPE_ERROR
        elif letters_voided or gift_aid_retractions:
            notif_type = Notification.TYPE_WARNING
        else:
            notif_type = Notification.TYPE_INFO

        recipients: dict[Any, Any] = {}

        def _add(user: Any) -> None:
            if user is not None and user.id not in recipients:
                recipients[user.id] = user

        _add(slip.processed_by)
        for donation in letters_voided:
            if donation.letter_batch is not None:
                _add(donation.letter_batch.created_by)
        seen_batches: set[Any] = set()
        for donation in donations:
            if donation.batch_id in seen_batches:
                continue
            seen_batches.add(donation.batch_id)
            _add(donation.batch.reviewed_by)
            _add(donation.batch.created_by)

        if not recipients:
            logger.info(
                "Reversal-cascade for slip %s had no recipients to notify",
                slip.slip_number,
            )
            return

        title = f"Banking reversal: {slip.slip_number}"
        client_name = slip.client.name if slip.client else "unknown client"

        # Plain-text body for in-app Notification.message — no escaping needed,
        # this is rendered as text in the bell-icon dropdown, not as HTML.
        plain_lines = [
            f"Slip {slip.slip_number} ({client_name}) "
            f"reversed {len(donations)} donation(s). Reason: {reason}.",
        ]
        if letters_voided:
            plain_lines.append(
                f"{len(letters_voided)} thank-you letter(s) marked voided — "
                "operator follow-up needed (donor contact / correction letter)."
            )
        if gift_aid_retractions:
            plain_lines.append(
                f"{len(gift_aid_retractions)} gift-aid donation(s) on already-submitted "
                "Gift Aid CSV(s) — HMRC retraction may be needed."
            )
        if card_refunds_pending:
            plain_lines.append(
                f"{len(card_refunds_pending)} card donation(s) reversed — issue Stripe "
                "refunds manually if appropriate; until then Stripe still holds these funds."
            )
        plain_message = " ".join(plain_lines)

        # HTML body for the email. Operator-controlled strings (slip_number,
        # client_name, reason, batch_name) flow through user input forms and
        # could contain ``<`` / ``>`` / quotes; ``format_html`` escapes them
        # so the email body cannot be HTML-injected by an operator typing
        # markup into a free-text reason field.
        html_parts = [
            format_html("<h2>Banking reversal: slip {}</h2>", slip.slip_number),
            format_html("<p><strong>Client:</strong> {}</p>", client_name),
            format_html("<p><strong>Reason:</strong> {}</p>", reason),
            format_html(
                "<p><strong>Donations reversed in this call:</strong> {}</p>",
                len(donations),
            ),
            mark_safe("<ul>"),
        ]
        if letters_voided:
            html_parts.append(
                format_html(
                    "<li><strong>{} thank-you letter(s) voided.</strong> "
                    "Operator follow-up: contact donor / send correction letter / "
                    "hold further runs.</li>",
                    len(letters_voided),
                )
            )
        if gift_aid_retractions:
            batch_names = sorted(
                {
                    d.batch.batch_name
                    for d in gift_aid_retractions
                    if d.batch is not None
                }
            )
            html_parts.append(
                format_html(
                    "<li><strong>{} gift-aid donation(s) on submitted "
                    "Gift Aid CSV(s)</strong> (batches: {}). "
                    "HMRC retraction may be required.</li>",
                    len(gift_aid_retractions),
                    format_html_join(", ", "{}", ((name,) for name in batch_names))
                    if batch_names
                    else "unknown",
                )
            )
        if card_refunds_pending:
            html_parts.append(
                format_html(
                    "<li><strong>{} card donation(s) reversed.</strong> "
                    "Issue Stripe refunds manually if appropriate; until then "
                    "Stripe still holds these funds.</li>",
                    len(card_refunds_pending),
                )
            )
        if not (letters_voided or gift_aid_retractions or card_refunds_pending):
            html_parts.append(
                mark_safe(
                    "<li>No downstream cascade impact (no letters issued, no "
                    "submitted Gift Aid CSV, no card donations).</li>"
                )
            )
        html_parts.append(mark_safe("</ul>"))
        html_message = format_html_join("\n", "{}", ((part,) for part in html_parts))

        # Build a slip-detail link for the in-app notification so clicking
        # the bell-icon entry routes to the page where the operator can act.
        try:
            slip_link = reverse("custom_admin:slip_detail", kwargs={"slip_id": slip.id})
        except Exception:
            logger.exception(
                "Failed to reverse slip_detail URL for slip %s; "
                "notifications will have no link.",
                slip.id,
            )
            slip_link = ""

        for user in recipients.values():
            try:
                Notification.objects.create(
                    user=user,
                    title=title,
                    message=plain_message,
                    notification_type=notif_type,
                    related_object_type="PayingInSlip",
                    related_object_id=str(slip.id),
                    link=slip_link,
                )
            except Exception:
                logger.exception(
                    "Failed to create reversal-cascade notification for user %s",
                    getattr(user, "id", "?"),
                )
            email = getattr(user, "email", "") or ""
            if email:
                send_batch_status_email.delay(
                    recipient_email=email,
                    subject=title,
                    html_message=html_message,
                )

    @staticmethod
    def generate_slip_number(client: Client, banking_date: Any) -> str:
        """Generate a unique slip number for the client and date.

        Format: {CLIENT_CODE}-{YYYYMMDD}-{SEQ}
        e.g., ABC-20260127-001

        Args:
            client: Client for the slip.
            banking_date: Date of banking.

        Returns:
            str: Generated unique slip number.
        """
        from banking.models import PayingInSlip

        client_code = "".join(c for c in client.name[:3] if c.isalnum()).upper()
        if not client_code:
            client_code = "XXX"

        date_str = banking_date.strftime("%Y%m%d")
        prefix = f"{client_code}-{date_str}-"

        existing_slips = PayingInSlip.objects.filter(
            slip_number__startswith=prefix
        ).order_by("-slip_number")

        if existing_slips.exists():
            last_slip = existing_slips.first()
            assert last_slip is not None
            try:
                last_seq = int(last_slip.slip_number.split("-")[-1])
                next_seq = last_seq + 1
            except ValueError, IndexError:
                next_seq = 1
        else:
            next_seq = 1

        return f"{prefix}{next_seq:03d}"

    @staticmethod
    def get_slip_clients(slip: PayingInSlip) -> list[Client]:
        """Return all unique clients from donations on this slip.

        Args:
            slip: PayingInSlip instance.

        Returns:
            list: Distinct Client instances.
        """
        from clients.models import Client

        return list(
            Client.objects.filter(campaigns__donations__paying_in_slip=slip).distinct()
        )
