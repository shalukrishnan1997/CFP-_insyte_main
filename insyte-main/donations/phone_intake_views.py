"""Operator-facing views for the phone donation intake console.

These views back ``/admin/phone-intake/`` and the small JSON endpoints it
posts to. The console is a single-page form rendered by
``templates/admin/donations/phone_intake.html``; HTMX/Alpine on the client
side handles donor search, inline donor creation, and donation
submission.

Architecture
------------
The actual creation logic lives in :mod:`donations.intake` so it can be
covered by unit tests without spinning up a request. The view layer here
is a thin parsing/serialisation shell on top of those service functions.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from campaigns.models import Campaign
from clients.models import Client
from core.models import User
from donations.intake import (
    PHONE_BATCH_NAME_PREFIX,
    PhoneDonationPayload,
    apply_phone_intake_auto_approval,
    create_phone_donation,
    create_phone_intake_batch,
    create_system_donor_inline,
    is_donation_auto_approve_eligible,
    process_phone_non_financial_intake,
)
from donations.models import Donation
from donors.models import DataFileDonor, Donor, SystemDonor
from donors.updates import PHONE_INTAKE_EDITABLE_FIELDS, DonorVanished
from responsehandling.permissions import has_permission_or_is_staff

logger = logging.getLogger(__name__)


# Whitelist of categorical reasons offered by the QA donation-review template
# (templates/admin/qa/donation_review.html). Mirrored here so the view can
# validate operator submissions without echoing arbitrary input.
NON_FINANCIAL_REASON_CHOICES: tuple[tuple[str, str], ...] = (
    ("in_kind", "In-Kind / Goods"),
    ("volunteering", "Volunteering"),
    ("pledge", "Pledge"),
    ("legacy", "Legacy / Bequest"),
    ("sponsorship", "Sponsorship"),
    ("other", "Other (see notes)"),
)
_NON_FINANCIAL_REASON_VALUES: frozenset[str] = frozenset(
    value for value, _ in NON_FINANCIAL_REASON_CHOICES
)


def _parse_decimal_amount(raw: str | None) -> Decimal | None:
    """Parse an operator-typed amount string into a Decimal.

    Returns ``None`` for unparseable input so the view can flag the
    donation rather than crash.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().lstrip("£").replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation, ValueError:
        return None


def _parse_optional_date(raw: str | None):
    """Parse an optional ISO date string. Returns None when empty/invalid."""
    if not raw:
        return None
    return parse_date(raw)


def _resolve_donor_for_donation(
    payload_donor_source: str,
    *,
    system_donor_id: str | None,
    donor_pk: str | None,
    data_file_donor_id: str | None,
) -> Donor | DataFileDonor | SystemDonor | None:
    """Look up the donor object referenced by the form payload.

    The phone-intake form's donor card carries one of three identifier
    fields based on which tier the donor came from. Return the matching
    ORM instance, or ``None`` when no donor is yet linked.
    """
    if system_donor_id:
        return SystemDonor.objects.filter(pk=system_donor_id).first()
    if donor_pk:
        return Donor.objects.filter(pk=donor_pk).first()
    if data_file_donor_id and payload_donor_source == "data_file":
        return DataFileDonor.objects.filter(pk=data_file_donor_id).first()
    return None


def _todays_batch_summary(*, campaign: Campaign, operator: User) -> dict[str, Any]:
    """Return today's running totals across all of this operator's calls.

    With one batch per call, the operator's UI counter aggregates over
    every phone batch they created today for *campaign*, rather than the
    one-daily-batch the previous model implied. Filtering by
    ``batch_name__startswith`` (instead of ``created_by``) keeps the
    grouping tightly scoped to operator-driven phone intake — system or
    webhook-driven status flips on unrelated batches don't pollute the
    counter.
    """
    from decimal import Decimal

    from django.db.models import Count, Sum

    from donations.models import Donation

    today = timezone.now().date()
    name_prefix = f"{PHONE_BATCH_NAME_PREFIX}{operator.username} — "

    aggregate = Donation.objects.filter(
        batch__batch_name__startswith=name_prefix,
        batch__campaign=campaign,
        created_at__date=today,
    ).aggregate(
        total_donations=Count("id"),
        total_amount=Sum("amount"),
    )
    total_donations = aggregate["total_donations"] or 0
    total_amount = aggregate["total_amount"] or Decimal("0.00")
    return {
        "exists": total_donations > 0,
        "total_donations": total_donations,
        "total_amount": f"{total_amount:.2f}",
    }


@has_permission_or_is_staff("donations.add_donation")
@require_http_methods(["GET"])
def phone_intake_help(request: HttpRequest) -> HttpResponse:
    """Render the operator playbook for the phone-intake console."""
    return render(request, "admin/donations/phone_intake_help.html", {})


@has_permission_or_is_staff("donations.add_donation")
@require_http_methods(["GET"])
def phone_intake_console(request: HttpRequest) -> HttpResponse:
    """Render the phone donation intake operator console.

    Query params:
        campaign_id: required UUID of the campaign for this call session.

    The template (``templates/admin/donations/phone_intake.html``) is a
    single-page operator form. All interactive behaviour (donor search,
    inline donor creation, donation submission) is HTMX/Alpine-driven and
    posts to the JSON endpoints below.
    """
    campaign_id = request.GET.get("campaign_id", "").strip()
    if not campaign_id:
        clients = (
            Client.objects.filter(campaigns__status=Campaign.STATUS_ACTIVE)
            .order_by("name")
            .distinct()
        )
        return render(
            request,
            "admin/donations/phone_intake_select_campaign.html",
            {"clients": clients},
        )

    campaign = get_object_or_404(
        Campaign.objects.select_related("client"), pk=campaign_id
    )

    # Best-effort Stripe publishable key lookup. If the client has no
    # gateway config the operator can still record cheque/cash/CAF/postal
    # donations — the card path is gated client-side on this being non-empty.
    # ``stripe_unavailable_reason`` carries the StripePaymentError message
    # through to the template so a staff operator hitting "Stripe is not
    # configured" sees the actual cause inline (instead of needing logs).
    from payments.services import StripePaymentError, StripePaymentService

    stripe_publishable_key = ""
    stripe_unavailable_reason = ""
    try:
        stripe_publishable_key = StripePaymentService._get_publishable_key(
            campaign.client
        )
    except StripePaymentError as exc:
        stripe_unavailable_reason = str(exc)
        logger.warning(
            "Stripe publishable key unavailable for client %s — card MOTO disabled: %s",
            getattr(campaign.client, "id", None),
            exc,
        )
    except Exception:
        stripe_unavailable_reason = "Unexpected server error — see logs."
        logger.exception(
            "Unexpected error resolving Stripe publishable key for client %s — card MOTO disabled",
            getattr(campaign.client, "id", None),
        )

    context: dict[str, Any] = {
        "campaign": campaign,
        "client": campaign.client,
        "today": timezone.now().date(),
        "todays_batch": _todays_batch_summary(
            campaign=campaign, operator=cast(User, request.user)
        ),
        "payment_method_choices": Donation.PAYMENT_METHOD_CHOICES,
        "frequency_choices": Donation.FREQUENCY_CHOICES,
        "donor_contact_status_choices": Donor.CONTACT_STATUS_CHOICES,
        "non_financial_reason_choices": NON_FINANCIAL_REASON_CHOICES,
        "donor_search_url": "/admin/api/donors/search/",
        "address_lookup_url": "/admin/api/address-lookup/",
        "stripe_publishable_key": stripe_publishable_key,
        "stripe_unavailable_reason": stripe_unavailable_reason,
    }
    return render(request, "admin/donations/phone_intake.html", context)


@has_permission_or_is_staff("donors.add_systemdonor")
@require_http_methods(["POST"])
def phone_intake_create_system_donor(request: HttpRequest) -> JsonResponse:
    """Create a SystemDonor inline during a phone call.

    Used when the operator searches for a donor and gets no hits. The
    operator types name + postcode + address, and this view creates a new
    SystemDonor with ``pending_review=True`` so the back office can
    verify it later.

    Expected POST body (JSON):
        campaign_id (str, required)
        first_name, last_name (str, required)
        title, email, phone, postcode, address_line1, address_line2,
        city, county, country (str, optional)
        consent_contact, opt_in_email/sms/phone/post (bool, optional)
        external_urn (str, optional — when caller already knows their URN)
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    campaign_id = (body.get("campaign_id") or "").strip()
    if not campaign_id:
        return JsonResponse({"error": "campaign_id is required"}, status=400)

    campaign = get_object_or_404(
        Campaign.objects.select_related("client"), pk=campaign_id
    )

    if not body.get("first_name") or not body.get("last_name"):
        return JsonResponse(
            {"error": "first_name and last_name are required"}, status=400
        )

    donor = create_system_donor_inline(
        campaign=campaign, operator=cast(User, request.user), payload=body
    )
    return JsonResponse(
        {
            "id": str(donor.id),
            "system_donor_id": str(donor.id),
            "external_urn": donor.external_urn,
            "first_name": donor.first_name,
            "last_name": donor.last_name,
            "full_name": donor.full_name,
            "phone": donor.phone,
            "postcode": donor.postcode,
            "pending_review": donor.pending_review,
            "source": "system_donor",
        },
        status=201,
    )


def _build_payload(body: dict[str, Any]) -> PhoneDonationPayload:
    """Translate POST JSON body into a strongly-typed PhoneDonationPayload."""
    payload: PhoneDonationPayload = {
        "amount": _parse_decimal_amount(body.get("amount")) or Decimal("0.00"),
        "currency": body.get("currency", "GBP"),
        "payment_method": body.get("payment_method", ""),
        "donation_date": _parse_optional_date(body.get("donation_date")),
        "gift_aid": bool(body.get("gift_aid", False)),
        "donation_frequency": body.get("donation_frequency", ""),
        "donor_source": body.get("donor_source", "house_file"),
        "cheque_number": body.get("cheque_number", ""),
        "cheque_date": _parse_optional_date(body.get("cheque_date")),
        "caf_voucher_number": body.get("caf_voucher_number", ""),
        "caf_amount": _parse_decimal_amount(body.get("caf_amount")) or Decimal("0.00"),
        "postal_order_number": body.get("postal_order_number", ""),
        "postal_order_date": _parse_optional_date(body.get("postal_order_date")),
        "sort_code": body.get("sort_code", ""),
        "account_number": body.get("account_number", ""),
        "direct_debit_start_date": _parse_optional_date(
            body.get("direct_debit_start_date")
        ),
        "card_holder_name": body.get("card_holder_name", ""),
        "card_last_four": body.get("card_last_four", ""),
        "card_expiry_date": body.get("card_expiry_date", ""),
        "donor_match_status": body.get("donor_match_status", "manual"),
        "dd_mandate_consent": body.get("dd_mandate_consent") or {},
        "bacs_validation_overridden": bool(
            body.get("bacs_validation_overridden", False)
        ),
        "non_financial_reason": (body.get("non_financial_reason") or "").strip(),
        "non_financial_notes": body.get("non_financial_notes") or "",
    }
    return payload


def _extract_donor_update_payload(body: dict[str, Any]) -> dict[str, object]:
    """Extract whitelisted ``donor_*`` fields from the POST body.

    Only keys that match :data:`PHONE_INTAKE_EDITABLE_FIELDS` are echoed
    through. Two helper keys (``donor_contact_status``,
    ``donor_contact_status_reason``) accompany the contact-status update.
    Postcode is normalised here so the donor row is consistent regardless
    of operator typing.
    """
    from core.services.postcode import _normalise_postcode

    extracted: dict[str, object] = {}
    for field in PHONE_INTAKE_EDITABLE_FIELDS:
        if field == "contact_status":
            continue  # paired keys handled below
        post_key = f"donor_{field}"
        if post_key in body:
            extracted[post_key] = body[post_key]

    # Contact-status block (handled as a pair).
    if "donor_contact_status" in body:
        extracted["donor_contact_status"] = body["donor_contact_status"]
        extracted["donor_contact_status_reason"] = body.get(
            "donor_contact_status_reason", ""
        )

    # Normalise the postcode (mirrors the address-lookup flow).
    if "donor_postcode" in extracted:
        raw_postcode = str(extracted["donor_postcode"] or "").strip()
        if raw_postcode:
            extracted["donor_postcode"] = _normalise_postcode(raw_postcode)

    return extracted


@has_permission_or_is_staff("donations.add_donation")
@require_http_methods(["POST"])
def phone_intake_create_donation(request: HttpRequest) -> JsonResponse:
    """Create a phone-intake donation in the daily-per-operator batch.

    Expected POST body (JSON):
        campaign_id (str, required)
        amount (str/decimal, required, > 0 for non-flagged status)
        payment_method (str, required)
        system_donor_id / donor_pk / data_file_donor_id (str, one optional)
        … plus payment-method-specific fields (cheque_number, sort_code …)

    Returns:
        201 with the created Donation summary on success.
        400 with an error message on validation failure.
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    campaign_id = (body.get("campaign_id") or "").strip()
    if not campaign_id:
        return JsonResponse({"error": "campaign_id is required"}, status=400)
    try:
        uuid.UUID(campaign_id)
    except ValueError:
        return JsonResponse({"error": "campaign_id is not a valid UUID"}, status=400)

    campaign = get_object_or_404(
        Campaign.objects.select_related("client"), pk=campaign_id
    )

    if not body.get("payment_method"):
        return JsonResponse({"error": "payment_method is required"}, status=400)
    if body["payment_method"] not in dict(Donation.PAYMENT_METHOD_CHOICES):
        return JsonResponse({"error": "Invalid payment_method"}, status=400)

    donor = _resolve_donor_for_donation(
        body.get("donor_source", "house_file"),
        system_donor_id=body.get("system_donor_id"),
        donor_pk=body.get("donor_pk"),
        data_file_donor_id=body.get("data_file_donor_id"),
    )

    payload = _build_payload(body)
    operator = cast(User, request.user)
    is_non_financial = (
        body.get("payment_method") == Donation.PAYMENT_METHOD_NON_FINANCIAL
    )

    # Donor contact-status validity is checked here for every payment
    # method, since the donor-edit surface is now available across the
    # board (PR #154). The non_financial_reason requirement remains on
    # the non-financial branch — it's about the call's purpose, not the
    # donor edit.
    contact_status = (body.get("donor_contact_status") or "").strip()
    if contact_status and contact_status not in dict(Donor.CONTACT_STATUS_CHOICES):
        return JsonResponse({"error": "Invalid donor_contact_status"}, status=400)

    if is_non_financial:
        if donor is None:
            return JsonResponse(
                {"error": "Donor required for non-financial intake"}, status=400
            )
        reason = payload.get("non_financial_reason", "")
        if not reason:
            return JsonResponse(
                {
                    "error": (
                        "non_financial_reason is required for non-financial donations"
                    )
                },
                status=400,
            )
        if reason not in _NON_FINANCIAL_REASON_VALUES:
            return JsonResponse({"error": "Invalid non_financial_reason"}, status=400)

    donor_update_payload = _extract_donor_update_payload(body)
    batch = create_phone_intake_batch(operator=operator, campaign=campaign)
    try:
        if is_non_financial:
            donation = process_phone_non_financial_intake(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=cast("Donor | DataFileDonor | SystemDonor", donor),
                donation_payload=payload,
                donor_update_payload=donor_update_payload,
            )
        else:
            donation = create_phone_donation(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                payload=payload,
                donor_updates=donor_update_payload,
            )
    except DonorVanished:
        return JsonResponse(
            {"error": ("Donor was deleted before submit; refresh and try again.")},
            status=410,
        )
    except ValidationError as exc:
        return JsonResponse({"error": "; ".join(exc.messages)}, status=400)

    requires_charge = (
        donation.payment_method == Donation.PAYMENT_METHOD_CARD
        and donation.amount > Decimal("0.00")
    )

    return JsonResponse(
        {
            "donation_id": str(donation.id),
            "qa_status": donation.qa_status,
            "payment_method": donation.payment_method,
            "amount": str(donation.amount),
            "currency": donation.currency,
            "batch_id": batch.id,
            "batch_name": batch.batch_name,
            "requires_charge": requires_charge,
            "todays_batch": _todays_batch_summary(campaign=campaign, operator=operator),
        },
        status=201,
    )


@has_permission_or_is_staff("donations.add_donation")
@require_http_methods(["POST"])
def phone_intake_charge(request: HttpRequest) -> JsonResponse:
    """Capture a card donation at phone intake (MOTO + auto-approve).

    Happy-path card donations capture synchronously and skip QA: on a
    clean charge the donation is promoted to ``qa_status=approved`` here
    in the same request so the operator sees "Charged £X" and the
    donation is letter-eligible immediately. Edge-case donations (DD
    flagged, pending-review donor, zero amount) are not handled by this
    endpoint — they don't trigger a card charge in the first place.

    3DS / declined / async-settlement paths leave the donation at
    ``qa_status=pending`` so the existing QA queue handles them; the
    Stripe ``payment_intent.succeeded`` webhook retro-approves a
    donation when SCA completes off-call.

    POST body (JSON):
        donation_id (str, required) — UUID of the just-created Donation.
        stripe_payment_method_id (str, required) — ``pm_xxx`` token from
            Stripe Elements; the backend never sees PAN or CVV.

    Returns:
        201 on success with:
            {
              "success": True,
              "payment_status": "completed" | "requires_capture" |
                                "awaiting_authentication",
              "auto_approved": bool,
              "payment_id": "...",
              "message": "...",
            }
        200 on declined card with ``error`` populated.
        4xx on validation failures.
    """
    from payments.batch_payment import BatchPaymentService

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    donation_id = (body.get("donation_id") or "").strip()
    payment_method_id = (body.get("stripe_payment_method_id") or "").strip()
    if not donation_id:
        return JsonResponse({"error": "donation_id is required"}, status=400)
    if not payment_method_id:
        return JsonResponse(
            {"error": "stripe_payment_method_id is required"}, status=400
        )

    donation = get_object_or_404(
        Donation.objects.select_related("campaign", "campaign__client"),
        pk=donation_id,
    )
    if donation.payment_method != Donation.PAYMENT_METHOD_CARD:
        return JsonResponse({"error": "Donation is not a card donation"}, status=400)

    # Eligibility for capture-at-intake + auto-approve. Recomputed from
    # the saved Donation fields (vs. the original payload, which is no
    # longer available here) so the rule is identical to the webhook
    # recovery path.
    eligible = is_donation_auto_approve_eligible(donation)

    operator = cast(User, request.user)
    result = BatchPaymentService.process_donation_payment(
        donation,
        operator,
        payment_method_id=payment_method_id,
        require_qa_approved=False,
        moto=True,
        capture_immediately=eligible,
    )

    donation.refresh_from_db()

    auto_approved = False
    # qa_status guard: a fast-arriving payment_intent.succeeded webhook
    # could have already promoted the donation between the charge call
    # and our refresh. Re-checking pending here keeps the view from
    # over-writing the webhook's qa_notes ("post-3DS authentication").
    if (
        result.get("success")
        and eligible
        and donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED
        and donation.qa_status == Donation.QA_STATUS_PENDING
    ):
        apply_phone_intake_auto_approval(
            donation,
            note="Auto-approved at phone intake — card captured live with donor",
        )
        auto_approved = True
    elif donation.qa_status == Donation.QA_STATUS_APPROVED:
        # Donation is already approved on refresh — almost always means the
        # webhook auto-approve fired between our charge call and refresh.
        # In theory a manual QA approval could land here too, but on phone
        # intake the donor is on the line within seconds, so the realistic
        # source is the webhook. Surfacing auto_approved=true keeps the
        # operator's success card consistent with the qa_status they see.
        auto_approved = True

    response_payload: dict[str, Any] = {
        "success": bool(result.get("success")),
        "donation_id": str(donation.id),
        "payment_status": donation.payment_status,
        "qa_status": donation.qa_status,
        "auto_approved": auto_approved,
        "payment_id": result.get("payment_id"),
        "message": result.get("message", ""),
    }
    if not result.get("success"):
        response_payload["error"] = result.get("error", "Charge failed")
        return JsonResponse(response_payload, status=200)

    if result.get("awaiting_authentication"):
        response_payload["awaiting_authentication"] = True

    return JsonResponse(response_payload, status=201)


@has_permission_or_is_staff("donations.change_donation")
@require_http_methods(["POST"])
def phone_intake_refund(request: HttpRequest) -> JsonResponse:
    """Refund a phone-intake card donation captured at intake.

    Same-session correction path: if an operator catches a typo (wrong
    amount, wrong donor) right after a card capture, this endpoint issues
    a Stripe refund and reverts the donation's ``qa_status`` to
    ``pending`` so it falls back into the QA queue rather than producing
    a thank-you letter.

    Restricted to:
      * the operator who took the donation, and
      * donations created within the last 24 hours.

    Late corrections are still possible via the standard donation admin's
    refund tooling — this view is purely the operator self-service
    surface.

    POST body (JSON):
        donation_id (str, required) — UUID of the donation to refund.
        reason (str, optional) — free text recorded with the refund.
    """
    from payments.services import StripePaymentError, StripePaymentService

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    donation_id = (body.get("donation_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not donation_id:
        return JsonResponse({"error": "donation_id is required"}, status=400)

    operator = cast(User, request.user)
    donation = get_object_or_404(
        Donation.objects.select_related("filled_by", "campaign"),
        pk=donation_id,
    )

    if donation.field_data.get("intake_method") != "phone":
        return JsonResponse(
            {"error": "Donation is not a phone-intake donation"}, status=400
        )

    one_day_ago = timezone.now() - timedelta(hours=24)
    if donation.created_at < one_day_ago:
        return JsonResponse(
            {"error": "Donation is older than 24h; use the standard refund admin"},
            status=400,
        )

    # Only the original operator can self-serve a refund. A different staff
    # user must use the donation admin (which records who issued the refund).
    if donation.filled_by_id != operator.id:
        return JsonResponse(
            {"error": "Only the original operator can refund via this endpoint"},
            status=403,
        )

    payment = donation.stripe_payments.order_by("-created_at").first()
    if payment is None:
        return JsonResponse({"error": "No Stripe payment to refund"}, status=400)

    try:
        refund_result = StripePaymentService.refund_payment(
            payment_id=str(payment.id),
            reason=reason or "Operator correction at phone intake",
        )
    except StripePaymentError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    except Exception as exc:
        logger.exception(
            "Unexpected error refunding phone-intake donation %s: %s",
            donation.id,
            exc,
        )
        return JsonResponse({"error": "Refund failed"}, status=500)

    donation.refresh_from_db()
    if donation.qa_status == Donation.QA_STATUS_APPROVED:
        donation.qa_status = Donation.QA_STATUS_PENDING
        donation.qa_notes = (
            f"Refunded at intake by {operator.username}: {reason or 'no reason given'}"
        )
        donation.save(update_fields=["qa_status", "qa_notes", "updated_at"])

    return JsonResponse(
        {
            "success": True,
            "donation_id": str(donation.id),
            "qa_status": donation.qa_status,
            "refund": refund_result,
        },
        status=200,
    )
